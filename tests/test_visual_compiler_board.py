from src.visual_compiler_board import _workflow, concept_board_prompt


def test_board_prompt_puts_declared_entities_before_long_concept():
    concept = (
        "TITLE: Toy Race\n\n"
        "ENTITIES (Count: 2 Total)\n"
        "1. **PlayerCar**: toy race car (kind: vehicle).\n"
        "2. **Coin**: spinning coin (kind: collectible).\n\n"
        "AI BEHAVIOR\n- follows the track.\n"
    )
    prompt = concept_board_prompt(concept)
    assert prompt.startswith("SHOW ALL DECLARED VISUAL ENTITIES:")
    assert prompt.index("PlayerCar") < prompt.index("SEMANTIC CONCEPT")
    assert prompt.index("Coin") < prompt.index("SEMANTIC CONCEPT")


def test_board_workflow_preserves_headless_comfy_settings():
    graph = _workflow(
        checkpoint="sd15ModelLab_realV001_1651730.safetensors",
        prompt="toy race car",
        negative="text",
        seed=978075547277410,
        width=768,
        height=768,
        steps=20,
        cfg=8.0,
        sampler_name="dpmpp_2m_sde_gpu",
        scheduler="karras",
        filename_prefix="board",
    )
    assert graph["4"]["inputs"]["ckpt_name"] == \
        "sd15ModelLab_realV001_1651730.safetensors"
    assert graph["3"]["inputs"]["sampler_name"] == "dpmpp_2m_sde_gpu"
    assert graph["3"]["inputs"]["scheduler"] == "karras"
    assert graph["3"]["inputs"]["steps"] == 20
    assert graph["3"]["inputs"]["cfg"] == 8.0
    assert graph["6"]["inputs"]["text"] == "toy race car"
    assert graph["7"]["inputs"]["text"] == "text"
