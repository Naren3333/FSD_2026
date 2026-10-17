"""Export versioned HTTP and Kafka schemas. Python HTTP schemas come from FastAPI.

With --check nothing is written: it lists every contract that differs from the code and exits 1 if any does.
"""

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


question = obj(
    {
        "id": UUID,
        "prompt": {"type": "string", "minLength": 1, "maxLength": 2000},
        "type": {"enum": ["multiple_choice", "numerical"]},
        "options": {"type": ["array", "null"], "items": TEXT},
        "answer": TEXT,
        "tolerance": {"type": "string"},
        "skill_id": UUID,
    }
)
evidence = obj(
    {
        "question_id": UUID,
        "skill_id": UUID,
        "correct": {"type": "boolean"},
        "awarded": {"type": "integer", "minimum": 0, "maximum": 1},
        "possible": {"const": 1},
    }
)
# Assessment and grading since ADR 0004: points, short answers, free responses with rubrics, drafts and dates.
# `question` and `evidence` above stay as the v1 events carry them.
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
    "skill_id": UUID,
}
question_v2 = obj(
    question_fields, ["id", "prompt", "type", "options", "answer", "tolerance", "points", "skill_id"]
)
# A criterion's id is assigned by Assessment, so a teacher sends criteria without one.
input_rubric = obj(
    {
        "criteria": array(
            obj({"id": TEXT, "title": TEXT, "levels": array(level, 2, 6)}, ["title", "levels"]), 1, 10
        ),
        "model_answer": {"type": "string", "maxLength": 5000},
    },
    ["criteria"],
)
# A question's id is optional and ignored: Assessment gives every question of a new or edited draft a new id.
question_input = obj(
    {
        **question_fields,
        "id": {**UUID, "description": "Ignored; every saved question gets a new id."},
        "rubric": input_rubric,
    },
    ["prompt", "type", "skill_id"],
)
public_question = obj(
    {k: question_fields[k] for k in ["id", "prompt", "type", "options", "max_length", "points", "skill_id"]},
    ["id", "prompt", "type", "options", "points", "skill_id"],
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
        "questions": array(question_v2, 1, 50),
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
            "description": "Omitted: published on create; on update the draft stays a draft (only an explicit "
            "`published` publishes it).",
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
    }
)
saved_answers = obj(
    {"answers": {"type": "object", "additionalProperties": {"type": "string"}}, "saved_at": NULLABLE_TIME}
)
ANSWERS = {"type": "object", "additionalProperties": {"type": "string"}}
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
    },
    ["status", "awarded"],
)
MARKS = {
    "question_id": UUID,
    "skill_id": UUID,
    "correct": {"type": "boolean"},
    "awarded": {"type": "integer", "minimum": 0, "maximum": 100},
    "possible": POINTS,
}
evidence_v2 = obj(
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
        "mime_type": {"enum": ["text/plain", "application/pdf"]},
        "status": {"enum": ["extracted", "extraction_unsupported", "indexing", "indexed", "failed"]},
        "revision": {"type": "integer", "minimum": 1},
        "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "doc_type": DOC_TYPE,
        "chunks": array(chunk, maximum=10000),
        "page_count": {"type": "integer", "minimum": 0},
        "chunk_count": {"type": "integer", "minimum": 0},
        "embedding_model": TEXT,
        "indexed_at": {"type": "string", "format": "date-time"},
        "failure_reason": {"enum": ["NO_TEXT_LAYER", "ENCRYPTED", "CORRUPT", "AI_UNAVAILABLE", "INTERNAL"]},
    },
    ["id", "classroom_id", "title", "mime_type", "status", "revision", "sha256", "doc_type"],
)
NULLABLE_TEXT = {"type": ["string", "null"]}
related = obj(
    {
        "chunk_id": UUID,
        "kind": {"enum": ["table", "figure", "text"]},
        "label": NULLABLE_TEXT,
        "page_start": {"type": "integer", "minimum": 1},
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
        "page_start": {"type": "integer", "minimum": 1},
        "page_end": {"type": "integer", "minimum": 1},
        "content": TEXT,
        "kind": {"enum": ["text", "table", "figure"]},
        "source": {"enum": ["pdf", "vision"]},
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
NODE_NAME = {"type": "string", "minLength": 1, "maxLength": 160}
node = obj(
    {
        "id": UUID,
        "kind": {"enum": ["subject", "course", "unit", "topic", "skill", "objective"]},
        "name": NODE_NAME,
        "parent_id": {"type": ["string", "null"], "format": "uuid"},
    }
)
evaluation = obj(
    {
        "id": UUID,
        "assessment_id": UUID,
        "classroom_id": UUID,
        "student_id": TEXT,
        "status": {"enum": ["draft", "finalized"]},
        "evidence": array(evidence_v2, 1, 50),
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
NAME = {"type": "string"}
student = obj({"id": TEXT, "username": NAME, "display_name": NAME})
enrollment = obj({"student_id": TEXT, "username": TEXT}, [])
enrollment["oneOf"] = [{"required": ["student_id"]}, {"required": ["username"]}]
error = obj({"error": obj({"code": TEXT, "message": TEXT})})


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
    """The error responses of a Go service's route: the standard envelope for each status."""
    return {
        str(s): {"description": ERRORS[s], "content": {"application/json": {"schema": error}}}
        for s in statuses
    }


def operation(method, path, body, response, code=200, query=()):
    parameters = [{"name": "id", "in": "path", "required": True, "schema": UUID}] if "{id}" in path else []
    parameters += (
        [{"name": "student", "in": "path", "required": True, "schema": TEXT}] if "{student}" in path else []
    )
    parameters += [
        {
            "name": q,
            "in": "query",
            "required": q not in ("student_id", "assessment_id"),
            "schema": {"student_id": TEXT, "title": TITLE}.get(q, UUID),
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
                + ([413] if body is not None else [])  # platform.Decode's 2 MiB limit
                + [422, 500, 503]
            ),
        },
    }
    if body is not None:
        op["requestBody"] = {"required": True, "content": {"application/json": {"schema": body}}}
    return path, method.lower(), op


# One page of the teacher's school's students, narrowed by an optional search.
school_students = operation(
    "GET",
    "/v1/students",
    None,
    obj({"students": array(student, maximum=50), "total": {"type": "integer", "minimum": 0}}),
)
school_students[2]["parameters"] = [
    {"name": "search", "in": "query", "required": False, "schema": {"type": "string", "maxLength": 120}},
    {"name": "offset", "in": "query", "required": False, "schema": {"type": "integer", "minimum": 0}},
    {
        "name": "limit",
        "in": "query",
        "required": False,
        "schema": {"type": "integer", "minimum": 1, "maximum": 50},
    },
]

operations = {
    "identity": [
        operation(
            "POST",
            "/v1/me",
            None,
            obj(
                {
                    "id": TEXT,
                    "org_id": TEXT,
                    "role": {"enum": ["teacher", "student"]},
                    "username": NAME,
                    "display_name": NAME,
                }
            ),
        ),
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
            obj({"student_id": TEXT, "classroom_id": UUID}),
        ),
        operation(
            "DELETE",
            "/v1/classrooms/{id}/enrollments/{student}",
            None,
            obj({"student_id": TEXT, "classroom_id": UUID}),
        ),
        operation("GET", "/v1/classrooms/{id}/students", None, array(student, maximum=500)),
        school_students,
    ],
    "curriculum": [
        operation("GET", "/v1/nodes", None, array(node, maximum=200)),
        operation("GET", "/v1/nodes/{id}", None, node),
        operation(
            "POST",
            "/v1/nodes",
            obj({k: v for k, v in node["properties"].items() if k != "id"}, ["kind", "name"]),
            node,
            201,
        ),
        operation("PATCH", "/v1/nodes/{id}", obj({"name": NODE_NAME}), obj({"id": UUID, "name": NODE_NAME})),
        operation("DELETE", "/v1/nodes/{id}", None, None, 204),
        operation(
            "POST",
            "/v1/skills/{id}/prerequisites",
            obj({"prerequisite_id": UUID}),
            obj({"skill_id": UUID, "prerequisite_id": UUID}),
        ),
    ],
    "content": [
        operation("GET", "/v1/documents", None, array(document), query=["classroom_id"]),
        operation("GET", "/v1/documents/{id}", None, document),
        operation("POST", "/v1/documents", None, document, 201, query=["classroom_id", "title"]),
        operation("POST", "/v1/documents/{id}/reindex", None, document, 202),
        operation("POST", "/v1/search", search_request, search_response),
    ],
    "assessment": [
        operation("GET", "/v1/assessments", None, array(assessment_summary), query=["classroom_id"]),
        operation("POST", "/v1/assessments", assessment_input, assessment, 201),
        operation(
            "GET",
            "/v1/assessments/progress",
            None,
            array(
                obj(
                    {
                        "id": UUID,
                        "title": TEXT,
                        "submissions": {"type": "integer", "minimum": 0},
                        "status": STATUS,
                        "due_at": NULLABLE_TIME,
                    }
                )
            ),
            query=["classroom_id"],
        ),
        operation("GET", "/v1/assessments/{id}", None, {"oneOf": [assessment, public_assessment]}),
        operation("PUT", "/v1/assessments/{id}", assessment_input, assessment),
        operation("DELETE", "/v1/assessments/{id}", None, obj({"id": UUID})),
        operation("POST", "/v1/assessments/{id}/publish", None, assessment),
        operation(
            "POST",
            "/v1/assessments/{id}/submissions",
            obj({"answers": {"type": "object", "additionalProperties": TEXT}}),
            obj({"id": UUID, "status": {"const": "submitted"}, "late": {"type": "boolean"}}),
            201,
        ),
        operation("GET", "/v1/assessments/{id}/saved-answers", None, saved_answers),
        operation("PUT", "/v1/assessments/{id}/saved-answers", obj({"answers": ANSWERS}), saved_answers),
    ],
    "grading": [
        operation(
            "GET",
            "/v1/evaluations",
            None,
            array(evaluation, maximum=500),
            query=["classroom_id", "assessment_id"],
        ),
        operation("GET", "/v1/evaluations/mine", None, array(own_result), query=["classroom_id"]),
        operation(
            "GET",
            "/v1/evaluations/counts",
            None,
            obj(
                {
                    "drafts": {"type": "integer", "minimum": 0},
                    "finalized": {"type": "integer", "minimum": 0},
                    "unreleased": {"type": "integer", "minimum": 0},
                }
            ),
            query=["classroom_id"],
        ),
        operation(
            "POST",
            "/v1/evaluations/release",
            obj({"assessment_id": UUID}),
            obj({"assessment_id": UUID, "released": {"type": "integer", "minimum": 0}}),
        ),
        operation(
            "POST",
            "/v1/evaluations/{id}/marks",
            obj({"marks": {"type": "object", "additionalProperties": mark, "minProperties": 1}}),
            evaluation,
        ),
        operation(
            "POST",
            "/v1/evaluations/{id}/finalize",
            obj(
                {
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "overrides": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 100},
                    },
                    "marks": {"type": "object", "additionalProperties": mark},
                },
                ["reason"],
            ),
            evaluation,
        ),
    ],
}
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
                for kind, size in [("text/plain", 5242880), ("application/pdf", 52428800)]
            },
        }
        upload["parameters"].append(
            {
                "name": "doc_type",
                "in": "query",
                "required": False,
                "schema": {**DOC_TYPE, "default": "teaching_material"},
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
            "description": "The caller's daily AI allowance is used up (ADR 0003)",
            "content": {
                "application/json": {
                    "schema": obj(
                        {
                            "error": obj(
                                {
                                    "code": {"const": "AI_SPEND_LIMIT"},
                                    "message": TEXT,
                                    "resets_at": {"type": "string", "format": "date-time"},
                                }
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
                            for kind in ["application/pdf", "text/plain"]
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
    save(
        f"contracts/http/{name}.openapi.json",
        {
            "openapi": "3.1.0",
            "info": {"title": name, "version": "1.0.0"},
            "security": [{"bearerAuth": []}],
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}}
            },
            "paths": paths,
        },
    )

payloads = {
    "identity.classroom.deleted.v1": obj({"classroom_id": UUID, "org_id": TEXT}),
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
            "page_count": {"type": "integer", "minimum": 0},
            "chunk_count": {"type": "integer", "minimum": 1},
            "embedding_model": TEXT,
        }
    ),
    "assessment.submitted.v1": obj(
        {
            "submission_id": UUID,
            "assessment_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "student_id": TEXT,
            "questions": array(question, 1, 50),
            "answers": {"type": "object", "additionalProperties": TEXT},
        }
    ),
    "assessment.submitted.v2": obj(
        {
            "submission_id": UUID,
            "assessment_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "student_id": TEXT,
            "questions": array(question_v2, 1, 50),
            "answers": {"type": "object", "additionalProperties": TEXT},
            "late": {"type": "boolean"},
            "submitted_at": {"type": "string", "format": "date-time"},
            "release_policy": RELEASE,
            "ai_marking": {"type": "boolean"},
            # The teacher whose AI allowance pays for AI marking; empty for assessments made before it was recorded.
            "teacher_id": {"type": "string"},
        }
    ),
    "grading.finalized.v1": obj(
        {
            "evaluation_id": UUID,
            "assessment_id": UUID,
            "classroom_id": UUID,
            "org_id": TEXT,
            "student_id": TEXT,
            "evidence": array(evidence, 1, 50),
        }
    ),
    "grading.finalized.v2": obj(
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
# When an event is published, for topics whose timing matters to consumers.
descriptions = {
    "grading.finalized.v2": "Published when a finalized grade becomes visible to its student: at finalization "
    "with release_policy on_finalize, otherwise when the teacher releases the assessment's results. Consumers "
    "show its marks to the student, and must deduplicate by evaluation_id.",
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
    )
    save(f"contracts/http/{name}.openapi.json", json.loads(data))
if CHECK and stale:
    raise SystemExit(
        "Out of date; run python scripts/export_contracts.py and review the diff:\n  " + "\n  ".join(stale)
    )
print(
    f"{'Checked' if CHECK else 'Exported'} ten HTTP contracts and {len(payloads)} implemented event schemas."
)
