"""Export versioned HTTP and Kafka schemas. Python HTTP schemas come from FastAPI."""

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


def save(path, data):
    (ROOT / path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


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
question_input = obj({k: v for k, v in question["properties"].items() if k != "id"})
public_question = obj({k: v for k, v in question["properties"].items() if k not in ["answer", "tolerance"]})
assessment = obj({"id": UUID, "classroom_id": UUID, "title": TEXT, "questions": array(question, 1, 50)})
public_assessment = {
    **assessment,
    "properties": {**assessment["properties"], "questions": array(public_question, 1, 50)},
}
evidence = obj(
    {
        "question_id": UUID,
        "skill_id": UUID,
        "correct": {"type": "boolean"},
        "awarded": {"type": "integer", "minimum": 0, "maximum": 1},
        "possible": {"const": 1},
    }
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
FIGURE = {"type": "string", "pattern": "^[a-f0-9]{16}\\.png$"}
document = obj(
    {
        "id": UUID,
        "classroom_id": UUID,
        "title": TEXT,
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
node = obj(
    {
        "id": UUID,
        "kind": {"enum": ["subject", "course", "unit", "topic", "skill", "objective"]},
        "name": TEXT,
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
        "evidence": array(evidence, 1, 50),
    }
)
own_result = obj(
    {
        **evaluation["properties"],
        "status": {"const": "finalized"},
        "finalized_at": {"type": "string", "format": "date-time"},
    }
)
NAME = {"type": "string"}
student = obj({"id": TEXT, "username": NAME, "display_name": NAME})
enrollment = obj({"student_id": TEXT, "username": TEXT}, [])
enrollment["oneOf"] = [{"required": ["student_id"]}, {"required": ["username"]}]
error = obj({"error": obj({"code": TEXT, "message": TEXT})})


def operation(method, path, body, response, code=200, query=()):
    parameters = [{"name": "id", "in": "path", "required": True, "schema": UUID}] if "{id}" in path else []
    parameters += (
        [{"name": "student", "in": "path", "required": True, "schema": TEXT}] if "{student}" in path else []
    )
    parameters += [
        {
            "name": q,
            "in": "query",
            "required": q != "student_id",
            "schema": TEXT if q in ("student_id", "title") else UUID,
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
            **{
                str(s): {"description": label, "content": {"application/json": {"schema": error}}}
                for s, label in [
                    (401, "Unauthenticated"),
                    (403, "Forbidden"),
                    (404, "Not found"),
                    (409, "State conflict"),
                    (422, "Validation error"),
                    (503, "Dependency unavailable"),
                ]
            },
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
            obj({}),
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
        operation("PATCH", "/v1/nodes/{id}", obj({"name": TEXT}), obj({"id": UUID, "name": TEXT})),
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
        operation(
            "GET", "/v1/assessments", None, array(obj({"id": UUID, "title": TEXT})), query=["classroom_id"]
        ),
        operation(
            "POST",
            "/v1/assessments",
            obj(
                {
                    "classroom_id": UUID,
                    "title": {"type": "string", "minLength": 1, "maxLength": 160},
                    "questions": array(question_input, 1, 50),
                }
            ),
            assessment,
            201,
        ),
        operation(
            "GET",
            "/v1/assessments/progress",
            None,
            array(obj({"id": UUID, "title": TEXT, "submissions": {"type": "integer", "minimum": 0}})),
            query=["classroom_id"],
        ),
        operation("GET", "/v1/assessments/{id}", None, {"oneOf": [assessment, public_assessment]}),
        operation(
            "POST",
            "/v1/assessments/{id}/submissions",
            obj({"answers": {"type": "object", "additionalProperties": TEXT}}),
            obj({"id": UUID, "status": {"const": "submitted"}}),
            201,
        ),
    ],
    "grading": [
        operation("GET", "/v1/evaluations", None, array(evaluation), query=["classroom_id"]),
        operation("GET", "/v1/evaluations/mine", None, array(own_result), query=["classroom_id"]),
        operation(
            "GET",
            "/v1/evaluations/counts",
            None,
            obj(
                {"drafts": {"type": "integer", "minimum": 0}, "finalized": {"type": "integer", "minimum": 0}}
            ),
            query=["classroom_id"],
        ),
        operation(
            "POST",
            "/v1/evaluations/{id}/finalize",
            obj(
                {
                    "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "overrides": {
                        "type": "object",
                        "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 1},
                    },
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
                    "404": {"description": "Not found", "content": {"application/json": {"schema": error}}},
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
                        "description": "Authorized original file",
                        "content": {
                            "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                        },
                    }
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
        {"$schema": "https://json-schema.org/draft/2020-12/schema", "$id": f"urn:learning:{topic}", **schema},
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
print(f"Exported ten HTTP contracts and {len(payloads)} implemented event schemas.")
