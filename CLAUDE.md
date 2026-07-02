Core Identity
This repository is a domain‑agnostic, multi‑backend compiler for procedural game content.
Claude is the semantic front‑end only.
All post‑schema steps are deterministic Python and must remain LLM‑free.

Backends: Unity and GTA5.

Pipeline Contract (Claude Must Follow This Exactly)
Claude participates only in Steps 2–3.

Prompt

Concept — Write a short, clear English brief

Schema — Fill the strict Pydantic schema (IR). Once valid, Claude stops.

Resolver — Deterministic ID lookup (Python)

Fixer — Deterministic consistency + naming

Generator — Deterministic backend codegen

Never modify Steps 4–6. Never introduce nondeterminism.

Atoms (New Required Mental Model)
Atoms are the smallest deterministic actions the compiler can execute.

Claude must treat atoms as:

primitive verbs

engine‑valid, schema‑valid, RAG‑grounded

indivisible (no branching, loops, or multi‑step logic)

safe building blocks for micro‑generators and scenes

Atoms live in:

atoms/ (definitions + registry)

micro/ (compositions built from atoms)

Claude must:

never invent new atom types

only use atoms defined in the schema

ensure atom outputs are deterministic

ensure atom sequences are valid for Unity/GTA backends

Atoms are the foundation of all microgens.

How Claude Should Behave
Treat the repo as a compiler, not a chat chain

Produce concise, unambiguous schema‑filling outputs

Never invent new fields or change schema structure

Never modify code unless explicitly asked

Prefer deterministic, reproducible transformations

Use Artifacts for diagrams, schema visualizations, and file edits

When editing files, produce full file replacements, not patches

Where Things Live (For Cowork Navigation)
Core
main.py — all commands; new features must be added here

config.yaml — model, RAG, Blender, Unity paths

Backends
GTA5 — pipeline.py, mission_schema.py, code_generator.py, resolver.py

Unity — unity_pipeline.py, unity_schema.py, unity_generator.py, unity_resolver.py, templates/unity/

Atoms + Microgens
atoms/ — atomic actions (movement, animation, camera, audio, FX, logic, entity)

micro/ — micro‑generators built from atoms

micro_pipeline.py — microgen 6‑step pipeline

templates/unity/micro/ — Unity microgen templates

Blender Asset Factory
blender_cli.py, src/blender/ — headless FBX generation

vehicle_generator.py, procedural_pipeline.py — parametric model builders

Web UI
webui/server.py — FastAPI UI

Claude should always reference these paths when generating or modifying files.
C:\Users\Owner\claude projects\GTA_mission_LLM\docs\model-pipeline-architecture.md
Model Pack v1 (Unity)
Claude must preserve this chain:

default_models.json → catalog

unity_model_resolver.py → resolves to Models/Defaults/<kind>/

unity_generator.py → copies + writes .meta

ModelSwap.cs.j2 / EntitySpawner.cs.j2 → runtime swap

Wiring uses entity_model_paths

Never break this chain.

Procedural 3D Models
Claude may generate:

{base, params} mappings

Blender scripts (bmesh builders)

FBX export instructions

But must not modify Blender internals unless asked.

Conventions Claude Must Enforce
Steps 4–6 must remain deterministic

Graceful degradation: missing model → primitive; missing Blender → stub

Config‑driven behavior (config.yaml)

Unity canonical script names must not change

Never break template references

Atoms must remain single‑action, deterministic, engine‑valid

Required Workflow Rule
After every new feature, Claude must remind the user:

“Open a GitHub Pull Request immediately.
No feature is considered complete until it is PR’d.”

This is part of the workflow contract.

How Claude Should Use Artifacts
Use Artifacts for:

diagrams (Mermaid/SVG)

file rewrites

schema visualizations

code generation previews

Artifacts must be self‑contained and runnable when possible

Prefer Mermaid for architecture diagrams

Prefer full‑file rewrites for code edits

Claude’s Output Style
Be concise

Use lists, tables, and diagrams

Avoid speculation

Never hallucinate schema fields

Always follow the compiler mental model

Always use atoms as the primitive layer