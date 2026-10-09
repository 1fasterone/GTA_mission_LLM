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

The requested micro `variant` is compiler-owned semantic authority. The fixer
repairs schema-valid values that contradict it (for example, a
`first_person` fill declaring `ThirdPersonChase`, or a `follow_2d` fill aiming
at `CameraFocus`) and records every correction in
`meta.camera_contract_repairs`. `cinemachine_topdown` intentionally accepts
only the two distinct identities `TopDown` and `Isometric`; an unrelated mode
is repaired to `TopDown`, the deterministic default. Presentation values that
are not implied by the variant — such as FOV, smoothing, and non-generic
top-down offsets — remain authored contract data.

## Runtime-enforced v1 surface

`CameraRig` is the sole camera-pose and target owner. The Unity camera micro
does not emit the generic atom-derived `CameraDefaultBehavior` companion:
that component's unconditional `LookAt(Player)` would overwrite a fixed 2D
angle or a first-person eye pose after the contract rig had applied it.

The generated `CameraRig` consumes:

- position base offset, optional distance clamps, and collision radius;
- rotation mode, pitch/yaw clamps, and roll behavior;
- base FOV and FOV clamps;
- smoothing and collision mode metadata;
- primary target type, declared reference, and resolved runtime transform;
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
| Third-person follow | named anchor resolves; CameraFocus is visible and the forward angle is within tolerance |
| First-person | `Player/Eye` resolves; camera copies eye pose; missing Eye fails loudly |
| 2D follow | orthographic projection, identity rotation, fixed depth, axis lock, and Player framing |
| Top-down/isometric | stable mode identity, deterministic contract offset, CameraFocus resolution, and visible target framing |
