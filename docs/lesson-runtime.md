# Lessons and trusted custom activities

Skills exposes an additive course composition API. Existing room, lecture and
challenge IDs, saved work, purchases, XP and heart authorities remain in their
existing services. No existing course is converted automatically.

## Read contracts

- `GET /courses/{id}/curriculum`: ordered chapters and lesson summaries, with
  activity IDs and derived completion; no activity content or private drafts.
- `GET /courses/{id}/lessons/{lesson}`: only the selected lesson's ordered
  activities, original room progress and resolved module descriptors.
- Course summaries/detail add `has_explicit_curriculum`. Only explicitly composed
  courses need the new outline to navigate; the summary never serializes the
  full curriculum.

Both new course reads enforce the existing verified account and course admission
rules and return private, non-cacheable responses. Lesson completion is derived
from the existing historical room/lecture records; it never creates XP. Skipping
an introduction preserves existing navigation semantics and never asserts
mastery. Starting a review does not erase historical completion.

Without `curriculum`, the adapter projects each existing room or lecture into
one lesson without rewriting IDs. For the exceptional mixed room/video ID
collision, only presentation IDs are deterministically qualified; every source
reference still carries its original persisted IDs. Legacy video practice is a
pointer resolved by the frontend only when that lesson opens; it is not loaded
for the whole catalogue and does not change historic video completion rules.

## Internal course authoring

The existing course JSON may add:

```json
{
  "curriculum": {
    "chapters": [{"id": "basics", "title": {"de": "Grundlagen", "en": "Basics"}}],
    "lessons": [{
      "id": "first-lesson",
      "title": {"de": "Erster Schritt", "en": "First step"},
      "chapter_id": "basics",
      "activities": [
        {"id": "existing-introduction", "source": {"kind": "room", "unit_id": "existing-introduction"}},
        {"id": "existing-exercise", "source": {"kind": "room", "unit_id": "existing-exercise"}}
      ]
    }]
  }
}
```

Chapters may be omitted; lesson and activity arrays define order. Room references
must belong to the course's existing `learning_path_id`; an activity retains its
unit ID. Optional localized `title` and `roles` (`explanation`, `practice`) adapt
presentation. Legacy lectures use source `{kind: "lecture", course_id,
section_id, lecture_id}` and the original video completion endpoint.

Authored exercises reference rooms with an `ExerciseRef` (`type`, `task_id`,
`subtask_id`). Direct authored `source:challenge` is rejected: challenge sources
exist only in the frontend legacy adapter. Completion still requires the existing
Challenges result, and a review requires its bound attempt proof. Multiple course
or future quest references to the same activity therefore share the same work
and original reward identity; composition introduces no reward of its own.

## Independently delivered modules

Apply migration `lessonmodules001` before registering modules. The migration
only adds `skills_lesson_modules`; it does not change user or content data.

Set `LESSON_MODULE_ORIGINS` to an explicit JSON list of trusted HTTPS origins.
Then validate and register the reviewed packager output:

```sh
python -m api.register_lesson_module /reviewed/package/module.json --check
python -m api.register_lesson_module /reviewed/package/module.json
```

`module.json` must contain exactly `{id, api_version: 1, entry_url}`. The API
version is the module protocol, not lesson/content versioning. `entry_url` points
to a standalone `.mjs` or `.js` ES module, without credentials, query, fragment or
path traversal. The separate package manifest holds artifact hashes; this CLI
imports the reviewed descriptor, does not download or execute the artifact and
has no public publishing endpoint. Replaying an identical registration is safe;
a changed artifact reference requires the explicit internal `--replace` option.
Runtime resolution rechecks the configured origin policy.

A unit uses `room: "custom"` and `module_id`; its public descriptor is resolved
from the registry only when opened. The web host loads the ES module on demand
and implements the shared mount/update/dispose contract. These are trusted
first-party modules; this is not a sandbox for user-supplied code. Static hosting
must serve the JavaScript MIME type, permit the app's module request through
CORS/CSP, and keep imported assets inside the reviewed package. The backend
allowlist does not itself validate redirects, transitive imports or runtime
behavior. For isolated local fixtures only, an explicitly allowed localhost
origin may use HTTP when `LESSON_MODULE_LOCAL_DEVELOPMENT=true`.

Custom and native video units use exactly one server completion authority:
`completion` for an introduction or `exercise` for a bound assessment (including
existing deployment exercise mappings). A native video stores each locale as
`{video: {type: "youtube", id: "11-character-id"}}` or
`{video: {type: "mp4", url: "https://..."}}`. A video introduction can confirm
`{viewed: true}`; it does not prove mastery or award lecture XP. Original lecture
activities keep their original video completion and XP behavior.

## Character areas

`GET /character-areas` returns configured areas with actual root skill IDs.
`GET /character-areas/{id}/skilltree` returns the existing skilltree shape for
that area. `/skilltree` remains unchanged. The initial packaged catalogue maps
all existing roots to IT without moving or creating skills.

A reviewed `CHARACTER_AREAS` JSON file can declare more areas with explicit
`root_skill_ids`, optional rows/columns, and at most one
`include_unassigned_roots` fallback. References and duplicate assignments are
validated; file changes require the normal process restart. Skill IDs, persisted
positions, bookmarks and relationships remain original. Visual edges outside
an area's selected roots are omitted from that projection only.

## Scope and checks

Targeted lesson reads load only their referenced room states. Course-context
room GET/mutations likewise select the requested unit, and course next-unit
selection selects the path's states. Outline completion reads only status
columns. The old continuous queue still needs broader history/prerequisites;
its catalogue deepcopy and external challenge checks under the user lock remain
separate performance work, not a solved scaling claim.

Tests cover course admission, ordered compound work, private state, exact retry,
review proof binding, preserved historical completion, module policy/registration,
additive migration and area/global-tree parity. A catalogue parity test iterates
all repository courses and their original room/lecture source order. Local test
results do not establish production load or deployment.
