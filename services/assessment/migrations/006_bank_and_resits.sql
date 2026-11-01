-- question bank and resits (adr 0017): each teacher's bank, which assessment questions came from it, and
-- resits assigned to chosen students. links and resit_of are plain references, not foreign keys, so deleting
-- a bank question or an original writes nothing to other rows.
-- additive only.
CREATE TABLE IF NOT EXISTS bank_questions(id uuid PRIMARY KEY, org_id text NOT NULL, owner_id text NOT NULL, question jsonb NOT NULL, topic_id uuid NOT NULL, type text NOT NULL, origin text, created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS bank_questions_owner_topic ON bank_questions(owner_id, topic_id);
CREATE INDEX IF NOT EXISTS bank_questions_owner_updated ON bank_questions(owner_id, updated_at DESC, id);

CREATE TABLE IF NOT EXISTS question_bank_links(assessment_id uuid NOT NULL REFERENCES assessments(id) ON DELETE CASCADE, question_id uuid NOT NULL, bank_question_id uuid NOT NULL, PRIMARY KEY(assessment_id, question_id));
CREATE INDEX IF NOT EXISTS question_bank_links_bank ON question_bank_links(bank_question_id);

-- fixed when the assessment is created: resits are assigned, every other assessment is for the whole class.
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS assigned boolean NOT NULL DEFAULT false;
ALTER TABLE assessments ADD COLUMN IF NOT EXISTS resit_of uuid;

CREATE TABLE IF NOT EXISTS assessment_assignees(assessment_id uuid NOT NULL REFERENCES assessments(id) ON DELETE CASCADE, student_id text NOT NULL, PRIMARY KEY(assessment_id, student_id));

-- for the student purge and the "answered" join.
CREATE INDEX IF NOT EXISTS submissions_student ON submissions(student_id);
CREATE INDEX IF NOT EXISTS assessment_assignees_student ON assessment_assignees(student_id);
