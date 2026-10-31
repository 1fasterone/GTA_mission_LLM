# Generated repository policy

- Treat this project as an isolated repository rooted at `I:\ActiveRepos\GTA_mission_LLM`.
- Intent producers append evidence; only `src/intent/resolver.py` chooses meaning.
- Validators report findings and never mutate schema.
- Generation renders canonical schema and never reinterprets prompt text.
- Preserve the Step 3.5 LLM freeze boundary and deterministic Steps 4-7.
- A generated Unity project must pass the real Editor compile gate before delivery.
