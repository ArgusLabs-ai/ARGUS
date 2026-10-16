// telemetry-intake — the only way a shape card gets into the telemetry DB.
//
// Public on purpose (deployed with verify_jwt = false): the sender is the
// open-source CLI, so there is no secret it could hold. What protects the DB:
//   1. size cap, then the published schema with additionalProperties: false —
//      a card with any field the schema does not list is rejected whole;
//   2. per-install and per-IP daily caps, counted in Postgres (tele_take);
//   3. the IP itself is never stored: the bucket is sha256(key | day | ip).
// See docs/DATA.md and src/argus/data/share_card.schema.json (this folder holds
// a byte-identical copy; tests/test_telemetry_intake.py keeps them in sync).

import Ajv2020 from "npm:ajv@8.17.1/dist/2020.js";
import { createClient } from "npm:@supabase/supabase-js@2.45.4";
import schema from "./share_card.schema.json" with { type: "json" };

const MAX_BYTES = 64 * 1024;
const INSTALL_CAP = 300; // the client stops at 200
const IP_CAP = 2000; // many CI jobs share one egress IP

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const db = createClient(SUPABASE_URL, SERVICE_KEY, { auth: { persistSession: false } });
const validate = new Ajv2020({ allErrors: false, strict: true }).compile(schema);

const reply = (status: number, body: Record<string, unknown>) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

async function ipBucket(req: Request): Promise<string> {
  const ip = (req.headers.get("x-forwarded-for") ?? "").split(",")[0].trim() || "unknown";
  const day = new Date().toISOString().slice(0, 10);
  const bytes = new TextEncoder().encode(`${SERVICE_KEY}|${day}|${ip}`);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `ip:${hex.slice(0, 32)}`;
}

async function underCaps(install: string, req: Request): Promise<boolean | null> {
  const checks = [
    db.rpc("tele_take", { buckets: [`i:${install}`], cap: INSTALL_CAP }),
    db.rpc("tele_take", { buckets: [await ipBucket(req)], cap: IP_CAP }),
  ];
  const results = await Promise.all(checks);
  if (results.some((r) => r.error)) return null;
  return results.every((r) => r.data === true);
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return reply(405, { error: "POST only" });

  const declared = Number(req.headers.get("content-length") ?? "0");
  if (declared > MAX_BYTES) return reply(413, { error: "card too large" });
  const raw = await req.text();
  if (raw.length > MAX_BYTES) return reply(413, { error: "card too large" });

  let card: Record<string, unknown>;
  try {
    card = JSON.parse(raw);
  } catch {
    return reply(400, { error: "not JSON" });
  }
  if (!validate(card)) {
    // Say where, never echo the value back.
    const e = validate.errors?.[0];
    return reply(422, { error: "card does not match schema", at: e?.instancePath, rule: e?.keyword });
  }

  const install = card.install as string;
  const allowed = await underCaps(install, req);
  if (allowed === null) return reply(503, { error: "try later" });
  if (!allowed) return reply(429, { error: "daily limit" });

  const { error } = await db.from("tele_cards").upsert(
    {
      install,
      run: card.run,
      fingerprint: card.fingerprint,
      status: card.status,
      argus_version: card.argus_version,
      langgraph_version: card.langgraph_version ?? null,
      python: card.python,
      ci: card.ci,
      n_steps: (card.steps as unknown[]).length,
      n_findings: (card.findings as unknown[]).length,
      card,
    },
    { onConflict: "install,run", ignoreDuplicates: true },
  );
  if (error) return reply(503, { error: "try later" });
  return reply(202, { ok: true });
});
