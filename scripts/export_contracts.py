"""export versioned http and kafka schemas."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UUID = {"type": "string", "format": "uuid"}
TEXT = {"type": "string", "minLength": 1}


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


def array(item, minimum=0, maximum=100):
    return {"type": "array", "items": item, "minItems": minimum, "maxItems": maximum}


CHECK = "--check" in sys.argv[1:]
stale = []  # with --check, the contracts that differ from the code


def save(path, data):
    text = json.dumps(data, indent=2) + "\n"
    target = ROOT / path
    if not CHECK:
        target.write_text(text, encoding="utf-8")
    elif not target.exists() or target.read_text(encoding="utf-8") != text:
        stale.append(path)


# assessment and grading since adr 0004: points, short answers, free responses with rubrics, drafts and
# dates.
NULLABLE_TIME = {"type": ["string", "null"], "format": "date-time"}
QUESTION_TYPES = {"enum": ["multiple_choice", "numerical", "short_answer", "free_response"]}
POINTS = {"type": "integer", "minimum": 1, "maximum": 100}
level = obj({"points": {"type": "integer", "minimum": 0, "maximum": 100}, "descriptor": TEXT})
criterion = obj({"id": TEXT, "title": TEXT, "levels": array(level, 2, 6)})
rubric = obj(
    {"criteria": array(criterion, 1, 10), "model_answer": {"type": "string", "maxLength": 5000}}, ["criteria"]
)
question_fields = {
    "id": UUID,
    "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
    "type": QUESTION_TYPES,
    "options": {"type": ["array", "null"], "items": TEXT},
    "answer": {"type": "string"},
    "tolerance": {"type": "string"},
    "accepted_answers": array(TEXT, 1, 20),
    "case_sensitive": {"type": "boolean"},
    "rubric": rubric,
    "max_length": {"type": "integer", "minimum": 1, "maximum": 20000},
    "points": POINTS,
    "topic_id": UUID,
}
QUESTION_REQUIRED = ["id", "prompt", "type", "options", "answer", "tolerance", "points", "topic_id"]
# marking suggestions may use teacher-supplied or retrieved marking-scheme passages.
excerpt = obj(
    {
        "document_id": UUID,
        "chunk_id": UUID,
        "page": {"type": "integer", "minimum": 1, "maximum": 100000},
        "text": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    ["text"],
)
question_fields = {**question_fields, "marking_scheme": array(excerpt, 1, 5)}
submitted_question = obj(question_fields, QUESTION_REQUIRED)
# since adr 0006 a question the teacher kept from ai question generation says so and names the passages it
# came from.
question_fields = {
    **question_fields,
    "sources": array(excerpt, 1, 5),
    "origin": {"enum": ["generated"], "description": "Left out for a question the teacher wrote."},
}
teacher_question = obj(question_fields, QUESTION_REQUIRED)
# a criterion's id is assigned by assessment, so a teacher sends criteria without one.
input_rubric = obj(
    {
        "criteria": array(
            obj({"id": TEXT, "title": TEXT, "levels": array(level, 2, 6)}, ["title", "levels"]), 1, 10
        ),
        "model_answer": {"type": "string", "maxLength": 5000},
    },
    ["criteria"],
)
# a question's id is optional and ignored: assessment gives every saved question a new id, except that a
# question left unchanged on a published assessment keeps the one it had.
question_input = obj(
    {
        **question_fields,
        "id": {
            **UUID,
            "description": "Ignored; every saved question gets a new id, except one left unchanged on a published "
            "assessment, which keeps its id and the work students saved on it.",
        },
        "rubric": input_rubric,
    },
    ["prompt", "type", "topic_id"],
)
public_question = obj(
    {k: question_fields[k] for k in ["id", "prompt", "type", "options", "max_length", "points", "topic_id"]},
    ["id", "prompt", "type", "options", "points", "topic_id"],
)
STATUS = {"enum": ["draft", "published"]}
LATE = {"enum": ["accept_flagged", "reject"]}
RELEASE = {"enum": ["on_finalize", "manual"]}
settings = {
    "status": STATUS,
    "opens_at": NULLABLE_TIME,
    "due_at": NULLABLE_TIME,
    "late_policy": LATE,
    "release_policy": RELEASE,
    "ai_marking": {"type": "boolean"},
}
assessment = obj(
    {
        "id": UUID,
        "classroom_id": UUID,
        "title": TEXT,
        "questions": array(teacher_question, 1, 50),
        **settings,
        "published_at": NULLABLE_TIME,
    }
)
assessment_input = obj(
    {
        "classroom_id": UUID,
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "questions": array(question_input, 1, 50),
        **settings,
        "status": {
            **STATUS,
            "description": "Omitted: published on create; on update it keeps its status (only an explicit "
            "`published` publishes a draft, and a published assessment cannot become a draft again).",
        },
    },
    ["classroom_id", "title", "questions"],
)
public_assessment = obj(
    {
        "id": UUID,
        "classroom_id": UUID,
        "title": TEXT,
        "questions": array(public_question, 1, 50),
        "opens_at": NULLABLE_TIME,
        "due_at": NULLABLE_TIME,
        "late_policy": LATE,
    }
)
assessment_summary = obj(
    {
        "id": UUID,
        "title": TEXT,
        "submitted": {"type": "boolean"},
        "status": STATUS,
        "opens_at": NULLABLE_TIME,
        "due_at": NULLABLE_TIME,
        "late_policy": LATE,
        "question_count": {"type": "integer", "minimum": 1, "maximum": 50},
        "points": {"type": "integer", "minimum": 1},
        "submissions": {
            "type": "integer",
            "minimum": 0,
            "description": "How many of the classroom's enrolled students have submitted it; for the classroom's "
            "teacher only.",
        },
        "has_submissions": {
            "type": "boolean",
            "description": "Whether any student has submitted it, students since removed from the classroom included: "
            "once one has, it can no longer be replaced, and deleting it needs with_submissions=true. For the "
            "classroom's teacher only.",
        },
    },
    ["id", "title", "submitted", "status", "opens_at", "due_at", "late_policy", "question_count", "points"],
)
saved_answers = obj(
    {"answers": {"type": "object", "additionalProperties": {"type": "string"}}, "saved_at": NULLABLE_TIME}
)
ANSWERS = {"type": "object", "additionalProperties": {"type": "string"}}
assessment_progress = obj(
    {
        "id": UUID,
        "title": TEXT,
        "submissions": {"type": "integer", "minimum": 0},
        "status": STATUS,
        "due_at": NULLABLE_TIME,
    }
)
assessment_details = obj(
    {
        "title": {"type": "string", "minLength": 1, "maxLength": 160},
        "due_at": NULLABLE_TIME,
        "late_policy": LATE,
    },
    [],  # a field left out keeps its value
)
submission_receipt = obj({"id": UUID, "status": {"const": "submitted"}, "late": {"type": "boolean"}})
own_submission = obj(
    {
        "answers": ANSWERS,
        "submitted_at": {"type": "string", "format": "date-time"},
        "late": {"type": "boolean"},
    }
)
criterion_score = obj({"criterion_id": TEXT, "points": {"type": "integer", "minimum": 0, "maximum": 100}})
suggestion = obj(
    {
        "status": {"enum": ["pending", "ready", "failed"]},
        "criteria": array(
            obj(
                {
                    "criterion_id": TEXT,
                    "points": {"type": "integer", "minimum": 0, "maximum": 100},
                    "evidence_quotes": array({"type": "string"}),
                    "rationale": {"type": "string"},
                }
            ),
            maximum=10,
        ),
        "awarded": {"type": "integer", "minimum": 0, "maximum": 100},
        "feedback": {"type": "string"},
        "confidence": {"enum": ["high", "medium", "low"]},
        "flags": array({"type": "string"}),
        "model": {"type": "string"},
        "reason": {"type": "string"},
        # how many of the question's marking-scheme passages the model was given (adr 0005).
        "scheme_passages": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    ["status", "awarded"],
)
MARKS = {
    "question_id": UUID,
    "topic_id": UUID,
    "correct": {"type": "boolean"},
    "awarded": {"type": "integer", "minimum": 0, "maximum": 100},
    "possible": POINTS,
}
evaluation_evidence = obj(
    {
        **MARKS,
        "needs_marking": {"type": "boolean"},
        "marked_by": {"enum": ["auto", "teacher"]},
        "criteria": array(criterion_score, maximum=10),
        "feedback": {"type": "string", "maxLength": 2000},
        "suggestion": suggestion,
    },
    list(MARKS),
)
final_evidence = obj(MARKS)
own_evidence = obj({**MARKS, "feedback": {"type": "string", "maxLength": 2000}}, list(MARKS))
mark = obj(
    {
        "criteria": {
            "type": "object",
            "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "awarded": {"type": "integer", "minimum": 0, "maximum": 100},
        "feedback": {"type": "string", "maxLength": 2000},
    },
    [],
)
chunk = obj(
    {
        "ordinal": {"type": "integer", "minimum": 0},
        "text": TEXT,
        "start_character": {"type": "integer", "minimum": 0},
        "end_character": {"type": "integer", "minimum": 1},
    }
)
DOC_TYPE = {"enum": ["teaching_material", "rubric", "marking_scheme"]}
TITLE = {"type": "string", "minLength": 1, "maxLength": 160}
FIGURE = {"type": "string", "pattern": "^[a-f0-9]{16}\\.png$"}
document = obj(
    {
        "id": UUID,
        "classroom_id": UUID,
        "title": TITLE,
        "mime_type": {
            "enum": [
                "text/plain",
                "application/pdf",
                "text/markdown",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ]
        },
        "status": {"enum": ["extracted", "extraction_unsupported", "indexing", "indexed", "failed"]},
        "revision": {"type": "integer", "minimum": 1},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "doc_type": DOC_TYPE,
        "chunks": array(chunk, maximum=10000),
        "page_count": {
            "type": "integer",
            "minimum": 0,
            "description": "PDF pages, PPTX slides, or internal Markdown/DOCX section units.",
        },
        "chunk_count": {"type": "integer", "minimum": 0},
        "embedding_model": TEXT,
        "indexed_at": {"type": "string", "format": "date-time"},
        "failure_reason": {
            "enum": ["NO_TEXT_LAYER", "ENCRYPTED", "CORRUPT", "TOO_LARGE", "AI_UNAVAILABLE", "INTERNAL"]
        },
    },
    ["id", "classroom_id", "title", "mime_type", "status", "revision", "sha256", "doc_type"],
)
NULLABLE_TEXT = {"type": ["string", "null"]}
related = obj(
    {
        "chunk_id": UUID,
        "kind": {"enum": ["table", "figure", "text"]},
        "label": NULLABLE_TEXT,
        "page_start": {
            "type": "integer",
            "minimum": 1,
            "description": "PDF page, PPTX slide, or internal Markdown/DOCX section anchor; interpret using source.",
        },
        "image": {"type": ["string", "null"], "pattern": FIGURE["pattern"]},
        "content": TEXT,
    }
)
search_hit = obj(
    {
        "chunk_id": UUID,
        "document_id": UUID,
        "title": TEXT,
        "doc_type": DOC_TYPE,
        "section_path": NULLABLE_TEXT,
        "page_start": {
            "type": "integer",
            "minimum": 1,
            "description": "PDF page, PPTX slide, or internal Markdown/DOCX section anchor; interpret using source.",
        },
        "page_end": {
            "type": "integer",
            "minimum": 1,
            "description": "Inclusive source-unit end; interpret using source.",
        },
        "content": TEXT,
        "kind": {"enum": ["text", "table", "figure"]},
        "source": {"enum": ["pdf", "vision", "markdown", "pptx", "docx"]},
        "image": {"type": ["string", "null"], "pattern": FIGURE["pattern"]},
        "score": {"type": "number"},
        "related": array(related),
    }
)
search_request = obj(
    {
        "classroom_id": UUID,
        "query": {"type": "string", "minLength": 1, "maxLength": 1000},
        "top_k": {"type": "integer", "minimum": 1, "maximum": 30, "default": 8},
        "rerank": {"type": "boolean", "default": True},
        "document_ids": array(UUID),
        "doc_types": array(DOC_TYPE, maximum=3),
    },
    ["classroom_id", "query"],
)
search_response = obj({"results": array(search_hit, maximum=30), "rerank_fallback": {"type": "boolean"}})
classroom = obj({"id": UUID, "name": TEXT, "teacher_id": TEXT})
TOPIC_NAME = {"type": "string", "minLength": 1, "maxLength": 160}
curriculum_topic = obj({"id": UUID, "name": TOPIC_NAME})
topic_input = obj({"name": TOPIC_NAME})
prerequisite = obj({"topic_id": UUID, "prerequisite_id": UUID})
evaluation = obj(
    {
        "id": UUID,
        "assessment_id": UUID,
        "classroom_id": UUID,
        "student_id": TEXT,
        "status": {"enum": ["draft", "finalized"]},
        "evidence": array(evaluation_evidence, 1, 50),
        "answers": ANSWERS,
        "late": {"type": "boolean"},
        "submitted_at": NULLABLE_TIME,
        "release_policy": RELEASE,
        "finalized_at": NULLABLE_TIME,
        "released_at": NULLABLE_TIME,
    }
)
own_result = obj(
    {
        "id": UUID,
        "assessment_id": UUID,
        "classroom_id": UUID,
        "student_id": TEXT,
        "status": {"const": "finalized"},
        "evidence": array(own_evidence, 1, 50),
        "answers": ANSWERS,
        "late": {"type": "boolean"},
        "finalized_at": {"type": "string", "format": "date-time"},
        "released_at": {"type": "string", "format": "date-time"},
    }
)
evaluation_page = obj(
    {"evaluations": array(evaluation, maximum=100), "total": {"type": "integer", "minimum": 0}}
)
grading_counts = obj(
    {
        "drafts": {"type": "integer", "minimum": 0},
        "finalized": {"type": "integer", "minimum": 0},
        "unreleased": {"type": "integer", "minimum": 0},
    }
)
release = obj({"assessment_id": UUID, "released": {"type": "integer", "minimum": 0}})
marks_request = obj({"marks": {"type": "object", "additionalProperties": mark, "minProperties": 1}})
finalize_request = obj(
    {
        "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
        "overrides": {
            "type": "object",
            "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "marks": {"type": "object", "additionalProperties": mark},
    },
    ["reason"],
)
NAME = {"type": "string"}
student = obj({"id": TEXT, "username": NAME, "display_name": NAME})
enrollment = obj({"student_id": TEXT, "username": TEXT}, [])
enrollment["oneOf"] = [{"required": ["student_id"]}, {"required": ["username"]}]
error = obj({"error": obj({"code": TEXT, "message": TEXT})})
profile = obj(
    {
        "id": TEXT,
        "org_id": TEXT,
        "role": {"enum": ["teacher", "student"]},
        "username": NAME,
        "display_name": NAME,
    }
)
enrollment_record = obj({"student_id": TEXT, "classroom_id": UUID})


ERRORS = {
    401: "Unauthenticated",
    403: "Forbidden",
    404: "Not found",
    409: "State conflict",
    413: "Too large",
    422: "Validation error",
    500: "Internal error",
    503: "Dependency unavailable",
}


def errors(statuses):
    """the error responses of a go service's route: the standard envelope for each status."""
    return {
        str(s): {"description": ERRORS[s], "content": {"application/json": {"schema": error}}}
        for s in statuses
    }


def operation(method, path, body, response, code=200, query=()):
    parameters = [{"name": "id", "in": "path", "required": True, "schema": UUID}] if "{id}" in path else []
    parameters += (
        [{"name": "student", "in": "path", "required": True, "schema": TEXT}] if "{student}" in path else []
    )
    parameters += (
        [{"name": "prerequisite", "in": "path", "required": True, "schema": UUID}]
        if "{prerequisite}" in path
        else []
    )
    parameters += [
        {
            "name": q,
            "in": "query",
            "required": q not in ("student_id", "assessment_id", "with_submissions"),
            "schema": {"student_id": TEXT, "title": TITLE, "with_submissions": {"type": "boolean"}}.get(
                q, UUID
            ),
        }
        for q in query
    ]
    op = {
        "operationId": method + path.replace("/", "_").replace("{", "").replace("}", ""),
        "parameters": parameters,
        "responses": {
            str(code): {
                "description": "Success",
                **({"content": {"application/json": {"schema": response}}} if response else {}),
            },
            **errors(
                [401, 403, 404, 409]
                + ([413] if body is not None else [])  # platform.decode's 2 mib limit
                + [422, 500, 503]
            ),
        },
    }
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return path, method.lower(), op


# the organization's active topics, or with archived=true its archived ones (still named by older questions
# and evidence), by name.
topic_list = operation("GET", "/v1/topics", None, array(curriculum_topic, maximum=200))
topic_list[2]["parameters"] = [
    {"name": "archived", "in": "query", "required": False, "schema": {"type": "boolean", "default": False}}
]

COUNT = {"type": "integer", "minimum": 0}


def paged(op, most, fallback):
    """the operation with the ?offset= and ?limit= of a list the server pages (platform.page)."""
    op[2]["parameters"] += [
        {
            "name": "offset",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 0, "default": 0},
        },
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "schema": {"type": "integer", "minimum": 1, "maximum": most, "default": fallback},
        },
    ]
    return op


# one page of the teacher's school's students, narrowed by an optional search.
school_student_page = obj({"students": array(student, maximum=50), "total": COUNT})
roster_page = obj({"students": array(student, maximum=500), "total": COUNT})
school_students = operation("GET", "/v1/students", None, school_student_page)
school_students[2]["parameters"] = [
    {"name": "search", "in": "query", "required": False, "schema": {"type": "string", "maxLength": 120}},
]
paged(school_students, 50, 10)

operations = {
    "identity": [
        operation("POST", "/v1/me", None, profile),
        operation("DELETE", "/v1/me", None, None, 204),
        operation("GET", "/v1/classrooms", None, array(classroom)),
        operation(
            "POST",
            "/v1/classrooms",
            obj({"name": {"type": "string", "minLength": 1, "maxLength": 120}}),
            classroom,
            201,
        ),
        operation("DELETE", "/v1/classrooms/{id}", None, obj({"classroom_id": UUID})),
        operation(
            "GET",
            "/v1/classrooms/{id}/access",
            None,
            obj({"allowed": {"const": True}, "relation": {"enum": ["teacher", "student"]}}),
            query=["student_id"],
        ),
        operation(
            "POST",
            "/v1/classrooms/{id}/enrollments",
            enrollment,
            enrollment_record,
        ),
        operation(
            "DELETE",
            "/v1/classrooms/{id}/enrollments/{student}",
            None,
            enrollment_record,
        ),
        # one page of the roster by name, and how many are enrolled.
        paged(
            operation("GET", "/v1/classrooms/{id}/students", None, roster_page),
            500,
            50,
        ),
        # every enrolled student's id, in the same order and not paged, for the services that keep removed
        # students' work out of the teacher's lists (platform.identity.roster).
        operation("GET", "/v1/classrooms/{id}/student-ids", None, {"type": "array", "items": TEXT}),
        school_students,
    ],
    "curriculum": [
        topic_list,
        operation("GET", "/v1/topics/{id}", None, curriculum_topic),
        operation("POST", "/v1/topics", topic_input, curriculum_topic, 201),
        operation("PATCH", "/v1/topics/{id}", topic_input, curriculum_topic),
        operation("DELETE", "/v1/topics/{id}", None, None, 204),
        operation("POST", "/v1/topics/{id}/restore", None, curriculum_topic),
        # the topic's active prerequisites, by name; links to archived topics are kept and listed again on
        # restore.
        operation("GET", "/v1/topics/{id}/prerequisites", None, array(curriculum_topic, maximum=200)),
        operation(
            "POST",
            "/v1/topics/{id}/prerequisites",
            obj({"prerequisite_id": UUID}),
            prerequisite,
        ),
        operation("DELETE", "/v1/topics/{id}/prerequisites/{prerequisite}", None, None, 204),
    ],
    "content": [
        operation("GET", "/v1/documents", None, array(document), query=["classroom_id"]),
        operation("GET", "/v1/documents/{id}", None, document),
        operation("POST", "/v1/documents", None, document, 201, query=["classroom_id", "title"]),
        operation("POST", "/v1/documents/{id}/reindex", None, document, 202),
        operation("DELETE", "/v1/documents/{id}", None, None, 204),
        operation("POST", "/v1/search", search_request, search_response),
    ],
    "assessment": [
        operation("GET", "/v1/assessments", None, array(assessment_summary), query=["classroom_id"]),
        operation("POST", "/v1/assessments", assessment_input, assessment, 201),
        operation(
            "GET",
            "/v1/assessments/progress",
            None,
            array(assessment_progress),
            query=["classroom_id"],
        ),
        operation("GET", "/v1/assessments/{id}", None, {"oneOf": [assessment, public_assessment]}),
        operation("PUT", "/v1/assessments/{id}", assessment_input, assessment),
        operation(
            "PATCH",
            "/v1/assessments/{id}",
            assessment_details,
            assessment,
        ),
        # once students have submitted, only with with_submissions=true: their submissions and grades go too
        # (adr 0008).
        operation("DELETE", "/v1/assessments/{id}", None, obj({"id": UUID}), query=["with_submissions"]),
        operation("POST", "/v1/assessments/{id}/publish", None, assessment),
        operation(
            "POST",
            "/v1/assessments/{id}/submissions",
            obj({"answers": {"type": "object", "additionalProperties": TEXT}}),
            submission_receipt,
            201,
        ),
        # the calling student's own submitted answers; 404 before they submit.
        operation(
            "GET",
            "/v1/assessments/{id}/submission",
            None,
            own_submission,
        ),
        operation("GET", "/v1/assessments/{id}/saved-answers", None, saved_answers),
        operation("PUT", "/v1/assessments/{id}/saved-answers", obj({"answers": ANSWERS}), saved_answers),
    ],
    "grading": [
        # one page of the enrolled students' evaluations, newest first, or of one assessment's, oldest
        # first.
        paged(
            operation(
                "GET",
                "/v1/evaluations",
                None,
                evaluation_page,
                query=["classroom_id", "assessment_id"],
            ),
            100,
            20,
        ),
        operation("GET", "/v1/evaluations/{id}", None, evaluation),
        operation("GET", "/v1/evaluations/mine", None, array(own_result), query=["classroom_id"]),
        operation(
            "GET",
            "/v1/evaluations/counts",
            None,
            grading_counts,
            query=["classroom_id", "assessment_id"],
        ),
        operation(
            "POST",
            "/v1/evaluations/release",
            obj({"assessment_id": UUID}),
            release,
        ),
        operation(
            "POST",
            "/v1/evaluations/{id}/marks",
            marks_request,
            evaluation,
        ),
        operation(
            "POST",
            "/v1/evaluations/{id}/finalize",
            finalize_request,
            evaluation,
        ),
    ],
}
# additive page endpoints preserve existing bounded array clients.
PAGE_SCHEMAS = {}
for service, resource, item, query in [
    ("identity", "classrooms", classroom, []),
    ("curriculum", "topics", curriculum_topic, []),
    ("content", "documents", document, ["classroom_id"]),
    ("assessment", "assessments", assessment_summary, ["classroom_id"]),
]:
    schema = obj({"items": array(item, maximum=100), "total": COUNT})
    PAGE_SCHEMAS[service] = schema
    page_op = paged(operation("GET", f"/v1/{resource}/page", None, schema, query=query), 100, 20)
    if service == "curriculum":
        page_op[2]["parameters"] += topic_list[2]["parameters"]
    operations[service].append(page_op)
operations["assessment"].append(
    operation(
        "POST",
        "/v1/assessments/batch",
        obj({"ids": array(UUID, 1, 50)}),
        array({"oneOf": [assessment, public_assessment]}, maximum=50),
    )
)

operations["assessment"].append(
    paged(
        operation(
            "GET",
            "/v1/assessments/progress/page",
            None,
            obj({"items": array(assessment_progress), "total": COUNT}),
            query=["classroom_id"],
        ),
        100,
        20,
    )
)

# the named schemas of the go services' contracts, under components/schemas.
NAMES = {
    "ClassroomPage": PAGE_SCHEMAS["identity"],
    "TopicPage": PAGE_SCHEMAS["curriculum"],
    "DocumentPage": PAGE_SCHEMAS["content"],
    "AssessmentPage": PAGE_SCHEMAS["assessment"],
    "Error": error,
    "Profile": profile,
    "Classroom": classroom,
    "Student": student,
    "StudentPage": roster_page,
    "SchoolStudentPage": school_student_page,
    "EnrollmentRequest": enrollment,
    "Enrollment": enrollment_record,
    "Topic": curriculum_topic,
    "TopicInput": topic_input,
    "Prerequisite": prerequisite,
    "Document": document,
    "Chunk": chunk,
    "SearchRequest": search_request,
    "SearchResponse": search_response,
    "SearchHit": search_hit,
    "RelatedItem": related,
    "Level": level,
    "Criterion": criterion,
    "Rubric": rubric,
    "RubricInput": input_rubric,
    "Excerpt": excerpt,
    "Question": teacher_question,
    "QuestionInput": question_input,
    "StudentQuestion": public_question,
    "Assessment": assessment,
    "AssessmentInput": assessment_input,
    "StudentAssessment": public_assessment,
    "AssessmentSummary": assessment_summary,
    "AssessmentProgress": assessment_progress,
    "AssessmentDetails": assessment_details,
    "SubmissionReceipt": submission_receipt,
    "OwnSubmission": own_submission,
    "SavedAnswers": saved_answers,
    "Evaluation": evaluation,
    "EvaluationEvidence": evaluation_evidence,
    "EvaluationPage": evaluation_page,
    "Suggestion": suggestion,
    "CriterionScore": criterion_score,
    "Mark": mark,
    "MarksRequest": marks_request,
    "FinalizeRequest": finalize_request,
    "OwnResult": own_result,
    "OwnEvidence": own_evidence,
    "GradingCounts": grading_counts,
    "Release": release,
}
NAMED = {id(schema): name for name, schema in NAMES.items()}
assert len(NAMED) == len(NAMES), "a schema has two names"


def referenced(node, used, top=None):
    """the node with every named schema in it (but `top` itself) replaced by its $ref, adding each name to `used`."""
    if isinstance(node, dict):
        if node is not top and id(node) in NAMED:
            used.add(NAMED[id(node)])
            return {"$ref": f"#/components/schemas/{NAMED[id(node)]}"}
        return {key: referenced(value, used) for key, value in node.items()}
    if isinstance(node, list):
        return [referenced(item, used) for item in node]
    return node


def components(used):
    """the named schemas `used` names and every named schema they refer to, by name."""
    schemas, pending = {}, set(used)
    while pending:
        name = pending.pop()
        if name not in schemas:
            refs = set()
            schemas[name] = referenced(NAMES[name], refs, top=NAMES[name])
            pending |= refs
    return dict(sorted(schemas.items()))


for name, routes in operations.items():
    paths = {}
    for path, method, op in routes:
        paths.setdefault(path, {})[method] = op
    if name == "content":
        upload = paths["/v1/documents"]["post"]
        upload["requestBody"] = {
            "required": True,
            "content": {
                kind: {"schema": {"type": "string", "format": "binary", "maxLength": size}}
                for kind, size in [
                    ("text/plain", 5242880),
                    ("text/markdown", 5242880),
                    ("application/pdf", 52428800),
                    ("application/vnd.openxmlformats-officedocument.presentationml.presentation", 52428800),
                    ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", 52428800),
                ]
            },
        }
        upload["parameters"].append(
            {
                "name": "doc_type",
                "in": "query",
                "required": False,
                # rubrics and marking schemes are no longer uploaded (adr 0009); documents keep their type.
                "schema": {"const": "teaching_material", "default": "teaching_material"},
            }
        )
        upload["responses"]["413"] = {
            "description": "Too large",
            "content": {"application/json": {"schema": error}},
        }
        upload["responses"]["400"] = {
            "description": "The upload did not finish",
            "content": {"application/json": {"schema": error}},
        }
        paths["/v1/documents/{id}/figures/{image}"] = {
            "get": {
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": UUID},
                    {"name": "image", "in": "path", "required": True, "schema": FIGURE},
                ],
                "responses": {
                    "200": {
                        "description": "A figure crop a search result points at",
                        "content": {"image/png": {"schema": {"type": "string", "format": "binary"}}},
                    },
                    **errors([401, 403, 404, 500, 503]),
                },
            }
        }
        paths["/v1/search"]["post"]["responses"]["429"] = {
            "description": "The caller's daily AI allowance is used up until resets_at (ADR 0003), or they have too "
            "many AI calls running at once (AI_BUSY)",
            "content": {
                "application/json": {
                    "schema": obj(
                        {
                            "error": obj(
                                {
                                    "code": {"enum": ["AI_SPEND_LIMIT", "AI_BUSY"]},
                                    "message": TEXT,
                                    "resets_at": {"type": "string", "format": "date-time"},
                                },
                                ["code", "message"],
                            )
                        }
                    )
                }
            },
        }
        paths["/v1/documents/{id}/original"] = {
            "get": {
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": UUID}],
                "responses": {
                    "200": {
                        "description": "Authorized original file, as uploaded",
                        "content": {
                            kind: {"schema": {"type": "string", "format": "binary"}}
                            for kind in [
                                "application/pdf",
                                "text/plain",
                                "text/markdown",
                                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            ]
                        },
                    },
                    **errors([401, 403, 404, 500, 503]),
                },
            }
        }
    for path in ["/health/live", "/health/ready"]:
        paths[path] = {
            "get": {
                "security": [],
                "responses": {
                    "200": {"description": "Healthy"},
                    "503": {"description": "Essential dependency unavailable"},
                },
            }
        }
    used = set()
    paths = referenced(paths, used)
    save(
        f"contracts/http/{name}.openapi.json",
        {
            "openapi": "3.1.0",
            "info": {"title": name, "version": "1.0.0"},
            "security": [{"bearerAuth": []}],
            "components": {
                "schemas": components(used),
                "securitySchemes": {
                    "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
                },
            },
            "paths": paths,
        },
    )

payloads = {
    "identity.classroom.deleted.v1": obj({"classroom_id": UUID, "org_id": TEXT}),
    "identity.enrollment.removed.v1": obj({"classroom_id": UUID, "org_id": TEXT, "student_id": TEXT}),
    "identity.user.deleted.v1": obj(
        {"user_id": UUID, "org_id": TEXT, "role": {"enum": ["teacher", "student"]}}
    ),
    "assessment.deleted.v1": obj({"assessment_id": UUID, "classroom_id": UUID, "org_id": TEXT}),
    "assessment.due_changed.v1": obj(
        {"assessment_id": UUID, "classroom_id": UUID, "org_id": TEXT, "due_at": NULLABLE_TIME}
    ),
    "content.uploaded.v1": obj(
        {
            "document_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "revision": {"type": "integer", "minimum": 1},
            "status": {"enum": ["extracted", "extraction_unsupported", "indexing"]},
        }
    ),
    "content.indexed.v1": obj(
        {
            "document_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "revision": {"type": "integer", "minimum": 1},
            "doc_type": DOC_TYPE,
            "page_count": {
                "type": "integer",
                "minimum": 0,
                "description": "PDF pages, PPTX slides, or internal Markdown/DOCX section units.",
            },
            "chunk_count": {"type": "integer", "minimum": 1},
            "embedding_model": TEXT,
        }
    ),
    "content.deleted.v1": obj({"document_id": UUID, "classroom_id": UUID, "org_id": TEXT}),
    "assessment.submitted.v4": obj(
        {
            "submission_id": UUID,
            "assessment_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "student_id": TEXT,
            "questions": array(submitted_question, 1, 50),
            "answers": {"type": "object", "additionalProperties": TEXT},
            "late": {"type": "boolean"},
            "submitted_at": {"type": "string", "format": "date-time"},
            "release_policy": RELEASE,
            "ai_marking": {"type": "boolean"},
            "teacher_id": {"type": "string"},
        }
    ),
    "grading.finalized.v3": obj(
        {
            "evaluation_id": UUID,
            "assessment_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "student_id": TEXT,
            "evidence": array(final_evidence, 1, 50),
        }
    ),
}
# when an event is published, for topics whose timing matters to consumers.
FINALIZED_TIMING = (
    "Published when a finalized grade becomes visible to its student: at finalization "
    "with release_policy on_finalize, otherwise when the teacher releases the assessment's results. Consumers "
    "show its marks to the student, and must deduplicate by evaluation_id."
)
descriptions = {
    "identity.user.deleted.v1": "A user deleted their own account (ADR 0011); the resource is the user. Assessment, "
    "Grading, Analytics and the chatbot delete the user's submissions, saved work, grades, evidence and chats, and "
    "refuse new ones for them. A teacher deletes their classrooms first.",
    "identity.enrollment.removed.v1": "A teacher removed a student from a classroom. The student's submissions and "
    "grades are kept, and leave the teacher's lists and counts while the student is not enrolled; Grading makes no "
    "more AI suggestions for work they submitted before.",
    "assessment.due_changed.v1": "A published assessment's teacher changed its due date, or removed it (null). "
    "Grading sets each evaluation's late flag again: late only when submitted after the new due date.",
    "assessment.deleted.v1": "A published assessment its teacher deleted, with any submissions (ADR 0008). Grading "
    "deletes its evaluations, Analytics its evidence, and both ignore anything for it that arrives later.",
    "grading.finalized.v3": FINALIZED_TIMING,
}
for topic, payload in payloads.items():
    schema = obj(
        {
            "event_id": UUID,
            "event_type": {"const": topic},
            "schema_version": {"const": 1},
            "timestamp": {"type": "string", "format": "date-time"},
            "correlation_id": UUID,
            "resource_id": UUID,
            "data": payload,
        }
    )
    save(
        f"contracts/events/{topic}.schema.json",
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"urn:learning:{topic}",
            **({"description": descriptions[topic]} if topic in descriptions else {}),
            **schema,
        },
    )

env = dict(os.environ, PYTHONPATH=str(ROOT / "packages/python-platform"))
for name in [
    "analytics",
    "ai-core",
    "question-generation-agent",
    "student-performance-agent",
    "question-chatbot-agent",
]:
    data = subprocess.check_output(
        [sys.executable, "-c", "import json; from main import app; print(json.dumps(app.openapi()))"],
        cwd=ROOT / "services" / name,
        env=env,
        text=True,
        encoding="utf-8",
    )
    save(f"contracts/http/{name}.openapi.json", json.loads(data))
# leftover event schemas (a retired version) are stale too: nothing above writes them.
written = {f"contracts/events/{topic}.schema.json" for topic in payloads}
stale += sorted(
    p.relative_to(ROOT).as_posix()
    for p in (ROOT / "contracts/events").glob("*.schema.json")
    if p.relative_to(ROOT).as_posix() not in written
)
if CHECK and stale:
    raise SystemExit(
        "Out of date; run python scripts/export_contracts.py and review the diff:\n  " + "\n  ".join(stale)
    )
if stale:  # leftover event schemas, when writing
    for path in stale:
        (ROOT / path).unlink()
        print(f"Removed {path}")
print(
    f"{'Checked' if CHECK else 'Exported'} ten HTTP contracts and {len(payloads)} implemented event schemas."
)
# the web app's api types are generated from these contracts (apps/web/src/api/generated); its check.py lane
# checks them with `npm run contracts:check`.
if not CHECK:
    web = ROOT / "apps/web"
    if (web / "node_modules").exists():
        npm = "npm.cmd" if os.name == "nt" else "npm"
        subprocess.check_call([npm, "run", "--silent", "contracts"], cwd=web)
    else:
        print(
            "apps/web has no node_modules: run npm ci there, then npm run contracts, to update the web's types."
        )
