-- each reassigned classroom's current teacher (adr 0019), who pays for its ai marking from then on.
-- additive only.
CREATE TABLE IF NOT EXISTS classroom_teachers(classroom_id uuid PRIMARY KEY, org_id text NOT NULL, teacher_id text NOT NULL, assigned_at timestamptz NOT NULL);
