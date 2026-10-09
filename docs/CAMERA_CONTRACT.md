# Camera Contract v1

The camera micro has one deterministic contract between schema, composition,
generated C#, and runtime acceptance. The Pipeline LLM may fill the contract in
Step 3; the fixer normalizes it before Step 6 renders Unity code.

## Ownership

In composed projects, the camera micro is the only camera-follow owner. The
legacy `CameraFollow` component is not attached when a composed camera unit is
present. The old flat fields (`offset_x`, `smooth_time`, and the other camera
fields) remain accepted for compatibility, but `default_fix()` migrates them
into the typed contract before rendering.

## Required identity and anchor

Every generated camera has a stable `mode_id`, `category`, and `priority`. The
anchor includes a typed `type`, a named `transform`, and a local `offset`.
First-person cameras require the controller-created `Player/Eye` transform;
they log an explicit error when it is missing instead of silently mounting on
the player root.

## Runtime-enforced v1 surface

The generated `CameraRig` consumes:

- position base offset, optional distance clamps, and collision radius;
- rotation mode, pitch/yaw clamps, and roll behavior;
- base FOV and FOV clamps;
- smoothing and collision mode metadata;
- primary target type;
- 2D axis lock, depth, orthographic size, and zoom bounds;
- validation toggles and tolerances;
- stable debug identity, color, and anchor gizmos.

The cinematic, secondary-target, blending, and response fields are typed in the
schema now so they can be promoted into runtime state transitions without
inventing a second schema shape. Their full state-machine behavior is a later
camera slice.

## Acceptance expectation

Camera acceptance must prove generated runtime behavior, not only that a Jinja
template contains a variant string. The minimum matrix is:

| Fixture | Required evidence |
|---|---|
| Third-person follow | named anchor resolves; camera follows and looks at focus |
| First-person | `Player/Eye` resolves; camera copies eye pose; missing Eye fails loudly |
| 2D follow | orthographic projection, identity rotation, fixed depth, axis lock |
| Top-down/isometric | deterministic contract offset and target framing |
