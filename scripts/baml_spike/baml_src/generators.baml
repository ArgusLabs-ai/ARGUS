// Spike-only generator. The generated client lands in
// scripts/baml_spike/baml_client/ and is checked in so the spike is
// reviewable and re-runnable without a codegen step.
//
// If BAML is adopted for real, this generator moves to the package root and
// the output_dir question from PROJECT.md §6 has to be settled properly
// (sdist exclusion list, CI codegen step). Nothing here is packaged.
generator spike {
    output_type "python/pydantic"
    output_dir "../"
    version "0.225.0"
    default_client_mode sync
}
