# Argus admin panel

Private panel over the telemetry database (`supabase/telemetry/`): what Argus
caught on users' runs, rule by rule. Two roles: one **owner**, any number of
**maintainers**. Live at https://argus-admin-mauve.vercel.app.

Access is enforced by row-level security in Postgres, not by this app: a
signed-in user who is not in `admin_users` reads nothing, and the publishable
key in `lib/supabase.ts` reads nothing on its own.

| Page | What it shows |
|---|---|
| Overview | Cards per day, share not clean, verdicts by Argus version |
| Rules | Each rule: fired, runs, installs, ignored (`argus ignore`), strict vs heuristic, versions |
| Runs | Every card, newest first; open one for findings, blame and each step's shape |
| Team | Owner adds or removes maintainers; maintainers see the list |

| Can… | Owner | Maintainer |
|---|---|---|
| Read cards, rules, runs | yes | yes |
| Add or remove maintainers | yes | no |
| Delete cards | yes | no |

## One-time setup

1. **Auth redirect.** Supabase dashboard → project `argus-telemetry` →
   Authentication → URL Configuration: set **Site URL** to
   `https://argus-admin-mauve.vercel.app` and add the same to **Redirect URLs**.
   Without this the sign-in link points at localhost.
2. **Owner.** Open the panel, request a sign-in link, click it. The panel says
   "No access yet". Then run once in the SQL editor:

   ```sql
   insert into public.admin_users (user_id, email, role)
   select id, email, 'owner' from auth.users where email = '<owner email>';
   ```

3. **Maintainer.** They request a link and sign in once; the owner adds their
   email on the Team page.

## Develop and deploy

```bash
cd admin
npm install
npm run dev            # http://localhost:3100 (add it to Redirect URLs to sign in locally)
vercel deploy --prod   # project varaddurges-projects/argus-admin
```
