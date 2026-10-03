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

## Private course content and module assets

Private teaching files must stay outside public source repositories, Nix source
closures and public static directories. No new database migration is needed.
Optional settings:

- `PRIVATE_COURSES_DIRECTORY`: an absolute directory containing reviewed
  `<course-id>.yml` files. These override only their matching IDs in the pinned
  `COURSES` catalogue, or add new IDs. Unspecified courses stay pinned. The
  directory and its YAML files must not be symlinks; invalid configuration or
  definitions fail startup. Do not change IDs, prices or existing access rights
  as a side effect of moving content.
- `LEARNING_ROOMS_CONTENT`: an absolute regular JSON file with the complete
  existing `Catalogue` schema. It replaces the packaged room catalogue, and is
  checked at startup. Missing, symlinked or invalid configured files fail closed;
  there is no fallback to public teaching material. Preserve unchanged paths,
  unit IDs and exercise bindings. Loaders cache at process startup; an operator
  validates and atomically publishes a complete bundle, then restarts Skills.
- `PRIVATE_LESSON_MODULES_ROOT`: private immutable packages at
  `<root>/<artifact-sha256>/`, readable by Skills and Nginx. Every component of
  this absolute filesystem path must be a real directory, not a symlink. Bind
  mounts are supported. Verify the actual host path before enabling it.
- `PRIVATE_LESSON_MODULE_GRANT_TTL`: seconds of asset access after a freshly
  authorized lesson/room response; default 3600, allowed 60–28800.

The pinned `COURSES` package remains in the system closure. A deployment can use
`/var/lib/academy-content/catalog/courses`,
`/var/lib/academy-content/catalog/learning_rooms.json` and
`/var/lib/academy-content/modules` for the private settings above. Persist these
directories and publish them privately; do not import their contents into Nix.

Public course list/summary responses still contain promotional metadata (course
description and translations, goals, prerequisites, titles, price and images).
Keep teaching material out of those metadata fields. Summaries exclude the
curriculum, section/lecture descriptions, video IDs/URLs and activity content.
Detailed courses, learning outlines, curricula and lessons require the existing
course admission. The full internal catalogue requires service authentication.
Previously published video IDs and teaching material remain in public Git
history; moving new content privately cannot retract those historical copies or
make externally hosted YouTube videos private.

Use the existing module descriptor/manifest and register through the same CLI.
A private registry `entry_url` is exactly the approved Skills public origin plus
`/private-lesson-modules/<artifact-sha256>/<encoded-entry.js-or-mjs>`. This reserved
URL is a reference, never a public file route. The immutable package contains
the matching `module.json`, `manifest.json` (`artifact_sha256`, original
`definition`, and `files` with `path`, `bytes`, `sha256`) and inventoried assets.
The private publisher verifies the complete package and content-address hash.
Private CLI checks also verify the local descriptor, inventory and entry bytes;
each asset request verifies its own exact inventory size/hash before serving.
Manifest metadata is limited to 1 MiB and individual assets to 512 MiB. Metadata,
unlisted/hidden files, symlinks and path traversal are never served. Keep package
files immutable and unwritable by the API/Nginx users, including between Skills'
check and Nginx's separate file open.

After verifying the current account and existing course admission, Skills
returns the same three-field descriptor with a runtime URL:
`PUBLIC_BASE_URL/lesson-assets/<grant>/<artifact-sha256>/<encoded-entry>`.
Relative ES imports and assets preserve the grant prefix. Native browser import
does not need an Authorization header or cross-origin credentials. The grant is
bound to user, course, unit and package; a module must belong to a real linked
course. Public modules retain their original behavior.

The opaque value is a domain-separated HMAC using the configured server secret;
only its expiring Redis record grants access. Authorized GET/save responses
renew that record while preserving the URL, including after Redis loss or a
long pause, so an assessment checkpoint does not remount the player. A previously
shared URL becomes usable again when its owner later opens the same content and
renews the grant: this is not permanent one-time expiration. Logout does not
immediately invalidate an issued grant. Without another authorized response,
late lazy assets can fail after expiry; reopening the lesson renews access.
Registry replacement preserves already issued old-package grants until their
existing expiry, so keep old immutable files. New responses issue only the new
package. Registry deletion or a local account-erasure tombstone denies existing
grants. Downloaded browser code remains copyable; this is access control, not DRM.

Required reverse-proxy contract (Skills does not serve raw bytes itself):

```nginx
# Never serve the canonical private registry references directly.
location ^~ /private-lesson-modules/ { return 404; }
location ^~ /_private-lesson-modules/ {
    internal;
    alias /var/lib/academy-content/modules/;
    disable_symlinks on;
    autoindex off;
    # Ensure application/javascript for both js and mjs, and correct asset MIME.
    # Preserve Cache-Control: private, no-store and Referrer-Policy: no-referrer.
    # Permit the Academy frontend's credential-free cross-origin module GET.
}
```

Proxy external `/skills/lesson-assets/` to the ordinary Skills router with GET
and HEAD support and no cache. Its successful response contains
`X-Accel-Redirect: /_private-lesson-modules/<hash>/<encoded-inventory-path>`.
Both external and internal asset locations must suppress access/error logs that
could include the capability URL; avoid tracing full grant paths at any CDN.
Skills redacts asset grant values from its Uvicorn/application logs and Sentry
events. Keep CORS/CSP and security headers in the actual evaluated Nginx config;
an API-only test of the redirect header does not prove byte delivery or CORS.
Disable public caching on success and failure, and reject direct internal URLs.

This feature changes neither prices nor native assessment authorization. The
existing Challenges APIs require a verified account and enabled task, but do
not universally enforce paid-course ownership. Before publishing new paid
native assessments, add the corresponding entitlement check at their own
list/detail/submission boundaries; a protected lesson URL alone cannot do it.

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
