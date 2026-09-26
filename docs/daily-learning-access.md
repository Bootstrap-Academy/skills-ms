# Daily learning access

Default: unchanged legacy behavior. `DAILY_LIMIT_POLICY_ENABLED=false` prevents
calls to the new backend policy API. The additive `dailystarts001` migration
creates the configuration row with `mode=off`, `lesson_limit=3`; restarting the
service never overwrites operator settings.

The backend's `GET /shop/_internal/learning-policy/{user_id}` is authoritative for
`legacy`, `shadow`, `daily` and actual Premium status. The local technical mode
is `off`, `shadow`, or `enforce`. No local setting upgrades an old contract to the
new model. `daily` with technical off/shadow stays daily (no hearts), with
`enforced=false`, `remaining=null`, and `can_start=true`. `unlimited=true` only
means an actual Premium/admin/course-purchase exemption. Backend outages never
invent an entitlement: global status returns 503; per-lesson status is unknown
(`daily=null`), and new work on an otherwise accessible course is preserved
without charging a slot. Retained-learning credentials keep their existing
scoped legacy authority and never acquire new contractual policy implicitly.

`GET /daily-limit`, lesson/curriculum reads and room reads never create a start.
`POST /courses/{course}/lessons/{lesson}/start` accepts `request_id: UUID` before
the first actual action. Room mutations perform the same admission themselves.
MP4 link reads in daily mode require an existing start and otherwise return
409 `lesson_start_required`; they never consume a lesson. Lectures are also
admitted on their completion endpoint. The Berlin calendar day determines the
counter, with midnight reset including DST. A durable start keeps the lesson
open across days and Premium expiry. Starts and mutation effects share a
transaction and the existing user/erasure lock. Request UUID reuse on another
lesson returns 409; same-lesson replays do not consume another slot.

Existing RoomState/lecture progress counts as begun. A skipped introduction on
its own does not. GETs recognize historical evidence without rewriting it;
the next mutation materializes a historical uncharged start. Operator-only
`POST /_internal/daily-limit/backfill/{user_id}` can materialize it beforehand,
idempotently under the same account lock. A deleted account stays deleted.
Starts/receipts are included in account exports and erasure. Original course
rights, LastWatch rows, XP and purchase evidence are unchanged. LastWatch remains
a legacy admission right, and does not create a paid-course quota exemption.

Grouping preserves older unit-as-lesson URLs as aliases and recognizes old
unit-keyed LessonStarts, including a start without a saved draft. Returned lesson
IDs and RoomEnvelope `course_id`/`lesson_id` identify the canonical group.

Internal configuration uses existing `aud=skills` authentication:

- `GET /_internal/daily-limit` returns mode, limit and activation issues.
- `PUT /_internal/daily-limit` accepts `{mode, limit, updated_by, note}`.
- Enforce requires real explicit curricula for every linked learning path; a
  boolean override is deliberately absent. Content must be grouped and reviewed
  before enforcement. Shadow measurement can run beforehand, but its unit-sized
  records are not evidence of the final grouped lesson distribution.

Challenges uses `POST /_internal/learning-access/{user_id}/check` for reads and
`/start` for actual actions. Body: nullable `task_id`/`subtask_id`, optional
`lecture_bindings: [{course_id, section_id?, lecture_id?}]`, trusted `user_admin`,
and a required request UUID for start. Only the authenticated Challenges service
supplies lecture bindings, derived from its CourseTask database, never a public
client. Skills independently resolves its own exact task/subtask catalogue
references and validates supplied course/section/lecture IDs. Shared work prefers
an already begun or purchased lesson. Reads remain browsable at the limit. A
course/section-wide task with no concrete lesson stays free practice after course
admission, per the product decision; an unknown supplied ID fails closed.

Historical exercise participation is read in batches from authenticated
`POST /challenges/_internal/users/{user_id}/learning-history`; the body contains
`subtask_ids` and exact `{course_id, lecture_id}` bindings, at most 500 combined.
The response contains only requested `attempted_subtask_ids` and
`attempted_lecture_bindings`, verified by Challenges from its own database,
including incorrect attempts. No public client claim or recursive public
Challenges request is used. Configure `INTERNAL_JWT_SECRET_CHALLENGES` for this
audience (empty retains the existing shared-key fallback). Deploy that read-only
endpoint before enabling Skills daily measurement/enforcement. Responses are
cached only for the current request; GETs do not materialize history. The
operator backfill and actual first mutation store the canonical grouped lesson.

An unavailable policy remains unknown/503 for unrelated new paid-course work;
known purchased, historical course and begun-lesson rights remain usable with
`daily:null`. A history failure never becomes evidence of a new quota charge.
Enforced admission requires that evidence; disabled and shadow operation retain
their existing access and leave the uncertain start uncharged. Known Premium,
purchases and local continuation do not need remote historical evidence.

Local verification:

```
nix develop --command pytest -q tests
nix develop --command poe mypy
DAILY_TEST_POSTGRES_BIN=/path/to/postgresql/bin nix develop --command python tests/run_daily_limit_postgres.py
```

The native runner creates and removes its own cluster and database, applies the
full migration chain, and verifies competing last-slot admissions, concurrent
idempotent retries and rollback. It does not use a product database. Public
activation, customer-contract transition, announcements and course publication
are separate reviewed operations.
