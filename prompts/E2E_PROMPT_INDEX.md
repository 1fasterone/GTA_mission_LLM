# Concept/image E2E prompt index

These files preserve the exact prompt inputs used by the image/text Concept comparison and feedback loop.

| File | Use |
| --- | --- |
| `e2e_image_only.txt` | Reusable Unity Concept carrier for every image-only run. The attached image is the only semantic input. |
| `e2e_turbo_toys.txt` | Verbatim copy of `turbo_toys.txt`, used by the text-brief Concept arm. |
| `e2e_round1_image_generation.txt` | Built-in image-generation prompt that produced `output/concept_loop/round1_winning_context.png` from the winning text-derived Concept. |
| `e2e_round2_image_generation.txt` | Built-in image-generation prompt that produced `output/concept_loop/round2_feedback_context.png` from the Round 1 Concept. |

The final full E2E run reused `e2e_image_only.txt` with `round2_feedback_context.png` attached. See `output/concept_loop/lineage.json` for the complete input/image/Concept/project chain.
