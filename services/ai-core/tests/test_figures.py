"""Figures: description, the parser, the chunker and /v1/ingest's crops.
The tests give the vision pass's results directly, use the mock OpenAI transport, or stub the vision pass."""

import asyncio
import base64
import json
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from conftest import FIGURE_LABELS, FakeOpenAI, ingested, make_figure_pdf, page_key0, with_vision
from fastapi.testclient import TestClient

from ingest import VisionConfig, figure_context, prompts, snap, vision, vision_calls
from ingest.chunker import DESCRIBED, chunk_paras
from ingest.figures import image_name
from ingest.headings import outline
from ingest.parse import read_pages
from ingest.vision import PageVision, SeenFigure, SeenTable

SHA = "cd" * 32
DESCRIPTION = "Three boxes in a row: the encoder feeds attention, which feeds the decoder."
PNG = b"\x89PNG crop one"
BEFORE = "The model has three parts. They are shown below."
AFTER = "Each part is trained jointly with the others."


def described(box, png: bytes = PNG, **kw) -> SeenFigure:
    return SeenFigure(
        box,
        "diagram",
        True,
        kw.pop("caption", "Figure 1: The encoder-decoder model."),
        description=DESCRIPTION,
        values=kw.pop("values", ()),
        words=" ".join(FIGURE_LABELS),
        png=png,
        **kw,
    )


def chat_reply(content: dict) -> dict:
    return {
        "id": "c1",
        "object": "chat.completion",
        "created": 0,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": json.dumps(content)},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def write(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "f.pdf"
    path.write_bytes(data)
    return path


# ---- describing

PAGE_AREA = 595.0 * 842.0


@pytest.mark.parametrize(
    ("figure", "ok"),
    [
        (SeenFigure((100, 100, 400, 300), "diagram", True), True),
        (SeenFigure((100, 100, 400, 300), "diagram", False), False),
        (SeenFigure((100, 100, 400, 300), "logo", True), False),
        (SeenFigure((100, 100, 400, 300), "illustration", True), False),
        (SeenFigure((100, 100, 120, 120), "chart", True), False),
    ],
    ids=["diagram", "decorative", "logo", "illustration", "tiny"],
)
def test_only_informative_figures_are_described(figure: SeenFigure, ok: bool) -> None:
    assert vision.describable(figure, [], PAGE_AREA) is ok


def test_a_figure_that_is_a_table_is_not_described() -> None:
    figure = SeenFigure((100, 100, 400, 300), "chart", True)
    assert not vision.describable(figure, [SeenTable((105, 102, 398, 296), 4, 3, "bordered")], PAGE_AREA)


def see_with_figures(tmp_path: Path, page_answer: dict) -> list:
    data, box = make_figure_pdf()
    path, cache = write(tmp_path, data), tmp_path / "cache"
    entry = vision_calls.cache_file(cache, "page-model", page_key0(path))
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps(page_answer), encoding="utf-8")
    return asyncio.run(
        vision.see_pages(
            path, model="page-model", cache_dir=cache, max_pages=500, figure_model="figure-model"
        )
    )


def figure_answer(box=None) -> dict:
    x0, y0, x1, y1 = box or make_figure_pdf()[1]
    return {
        "tables": [],
        "figures": [
            {
                "bbox": [x0 / 595, y0 / 842, x1 / 595, y1 / 842],
                "type": "diagram",
                "caption": "Figure 1: The encoder-decoder model.",
                "informative": True,
            }
        ],
    }


def test_a_figure_is_described_from_its_crop_and_words(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(
        json=chat_reply({"useful": True, "description": DESCRIPTION, "key_values": ["layers ≈ 6"]})
    )
    [page] = see_with_figures(tmp_path, figure_answer())
    [figure] = page.figures
    assert figure.description == DESCRIPTION and figure.values == ("layers ≈ 6",)
    assert figure.words.split() == list(FIGURE_LABELS) and figure.png.startswith(b"\x89PNG")
    [request] = openai_fake.requests
    prompt = request["messages"][0]["content"][0]["text"]
    assert (
        request["model"] == "figure-model" and "Encoder Attention Decoder" in prompt and "Figure 1" in prompt
    )
    key = page_key0(tmp_path / "f.pdf")[: vision_calls.KEY_CHARS]
    assert list(
        (tmp_path / "cache" / "figure-model").glob(f"pg-{key}-f0-d{prompts.DESCRIBE_PROMPT_VERSION}-*.json")
    )


def test_a_figure_the_model_finds_useless_stays_undescribed(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_reply({"useful": False, "description": "A company logo.", "key_values": []}))
    [page] = see_with_figures(tmp_path, figure_answer())
    assert page.figures[0].description is None and page.figures[0].png is None


def test_a_failed_description_leaves_the_page_and_its_figure_alone(
    tmp_path: Path, openai_fake: FakeOpenAI
) -> None:
    openai_fake.reply(400, json={"error": {"message": "bad image", "type": "invalid_request_error"}})
    [page] = see_with_figures(tmp_path, figure_answer())
    assert page is not None and page.figures[0].description is None


# ---- parser, outline, chunker


def test_figure_words_and_caption_leave_the_prose(tmp_path: Path) -> None:
    data, box = make_figure_pdf(before=BEFORE, after=AFTER)
    doc = read_pages(write(tmp_path, data), [PageVision([], [described(box)])])
    prose = " ".join(b.text for b in doc.pages[0])
    assert not any(label in prose for label in FIGURE_LABELS) and "Figure 1" not in prose
    assert "three parts" in prose and "trained jointly" in prose
    [[figure]] = doc.figures
    assert figure.description == DESCRIPTION and figure.image == image_name(PNG)


def test_a_whole_page_figure_keeps_its_words_in_the_prose(tmp_path: Path) -> None:
    data, box = make_figure_pdf(caption=None, full_page=True)
    doc = read_pages(write(tmp_path, data), [PageVision([], [described(box, caption=None)])])
    prose = " ".join(b.text for b in doc.pages[0])
    assert all(label in prose for label in FIGURE_LABELS)
    assert doc.figures[0][0].strip is False


def test_a_figure_is_a_chunk_of_its_own(tmp_path: Path) -> None:
    data, box = make_figure_pdf(before=BEFORE, after=AFTER)
    doc = read_pages(write(tmp_path, data), [PageVision([], [described(box, values=("layers ≈ 6",))])])
    chunks = chunk_paras(outline(doc.pages, doc.tables, doc.figures))
    assert [c.kind for c in chunks] == ["text", "figure"]  # the text around a figure stays together
    assert "three parts" in chunks[0].content and "trained jointly" in chunks[0].content
    figure = chunks[1]
    assert figure.source == "vision" and figure.image == image_name(PNG) and figure.page_start == 1
    lines = figure.content.split("\n")
    assert lines[0] == "Figure 1: The encoder-decoder model." and f"[diagram, {DESCRIBED}]" in figure.content
    assert DESCRIPTION in figure.content and "Key values (approximate): layers ≈ 6" in figure.content
    assert "Labels: Encoder Attention Decoder" in figure.embed_body and "Labels:" not in figure.content


def test_a_figure_without_a_caption_borrows_nothing(tmp_path: Path) -> None:
    data, box = make_figure_pdf(caption=None, before=BEFORE)
    doc = read_pages(write(tmp_path, data), [PageVision([], [described(box, caption=None)])])
    figure = next(c for c in chunk_paras(outline(doc.pages, doc.tables, doc.figures)) if c.kind == "figure")
    assert figure.content.startswith(f"[diagram, {DESCRIBED}]")


# ---- POST /v1/ingest (storing crops and serving images moves to Content)


@pytest.fixture
def figures_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """The vision pass stubbed to describe the diagram; `crop["png"]` is the crop it returns."""
    crop = {"png": PNG}

    async def see_pages(path, **kw):
        assert kw["figure_model"] == "figure-model"
        return [PageVision([], [described(make_figure_pdf()[1], png=crop["png"])])]

    with_vision(monkeypatch, VisionConfig("page-model", tmp_path / "vision", 500, "figure-model"))
    monkeypatch.setattr(vision, "see_pages", see_pages)
    return crop


def figure_doc(client: TestClient) -> tuple[dict, dict]:
    doc = ingested(client, make_figure_pdf(before=BEFORE, after=AFTER)[0])
    assert doc["outcome"] == "indexed", doc
    [figure] = [c for c in doc["chunks"] if c["kind"] == "figure"]
    return doc, figure


def test_a_figure_comes_back_with_its_crop(api: TestClient, embeddings: FakeOpenAI, figures_on: dict) -> None:
    doc, figure = figure_doc(api)
    assert figure["source"] == "vision" and figure["image"] == image_name(PNG)
    assert doc["figures"] == [{"image": image_name(PNG), "png_base64": base64.b64encode(PNG).decode()}]
    assert "Labels: Encoder Attention Decoder" in figure["embed_text"]
    assert all(c["image"] is None for c in doc["chunks"] if c["kind"] != "figure")


def test_a_new_crop_gets_a_new_name(api: TestClient, embeddings: FakeOpenAI, figures_on: dict) -> None:
    _, old = figure_doc(api)
    figures_on["png"] = b"\x89PNG crop two"
    doc, new = figure_doc(api)
    assert new["image"] == image_name(b"\x89PNG crop two") != old["image"]
    assert [f["image"] for f in doc["figures"]] == [new["image"]]


# ---- snapping a figure's box to its content

PAGE = (612.0, 792.0)
CHART = (
    73.0,
    67.0,
    285.0,
    243.0,
)  # the model's box for CLIP's Figure 7: labels cut at the left, no axis title


def chart_lines() -> list:
    return [
        ((55.0, 48.0, 342.0, 57.0), "Learning Transferable Visual Models", 9.0),  # running header
        ((55.0, 150.0, 110.0, 158.0), "KITTI Distance", 6.4),  # cut by the box's left edge
        ((104.0, 241.0, 289.0, 252.0), "0 25 50 75 100 125 150 175 200", 6.4),  # ticks just below
        ((144.0, 250.0, 246.0, 261.0), "# of labeled examples per class", 6.4),  # axis title, below the ticks
        ((150.0, 257.0, 239.0, 268.0), "required to match zero-shot", 6.4),
        ((55.0, 280.0, 289.0, 289.0), "Figure 7. Thedataefficiencyofzero-shottransfervaries", 9.0),
        ((55.0, 291.0, 289.0, 300.0), "widely.Calculatingthenumberoflabeled", 9.0),  # caption, glued words
        ((304.0, 113.0, 316.0, 207.0), "Zero-Shot CLIP Performance", 6.6),  # the next chart's axis title
        *[
            (
                (55.0, 400.0 + 12 * n, 289.0, 410.0 + 12 * n),
                "Body text of the paper, well away from the chart and much longer than any label.",
                10.0,
            )
            for n in range(6)
        ],
    ]


def test_a_chart_box_grows_to_its_labels_and_axis_title() -> None:
    snapped = snap.snap(CHART, [], chart_lines(), [(324.0, 68.0, 518.0, 243.0)], PAGE)
    assert snapped == (55.0, 67.0, 289.0, 268.0)


def test_snapping_leaves_out_headers_captions_and_the_neighbours_labels() -> None:
    snapped = snap.snap(CHART, [], chart_lines(), [(324.0, 68.0, 518.0, 243.0)], PAGE)
    assert snapped[1] > 57.0 and snapped[3] < 280.0 and snapped[2] < 304.0
    alone = snap.snap(
        CHART, [], chart_lines(), [], PAGE
    )  # with no figure beside it, the axis title is its own
    assert alone[2] == 316.0


def test_snapping_takes_in_the_drawings_it_mostly_covers_but_not_backgrounds() -> None:
    legend = (260.0, 100.0, 300.0, 140.0)  # 3/4 inside the (padded) box
    background = (0.0, 0.0, 612.0, 792.0)
    assert snap.snap(CHART, [legend, background], [], [], PAGE) == (73.0, 67.0, 300.0, 243.0)


def test_snapping_grows_at_most_snap_grow_a_side() -> None:
    far = (CHART[0], CHART[1], CHART[2] + 100, CHART[3])  # mostly inside, but 100pt past the edge
    assert snap.snap(CHART, [far], [], [], PAGE) == CHART


# ---- figure context and the photo gate

PAPER = [
    [
        "Results",
        "In addition to the averages, we look at each dataset. In Figure 7, we show how many",
        "labeled examples per class a linear probe needs to match zero-shot. Other results follow.",
    ],
    [
        "Figure 7. The data efficiency of zero-shot transfer varies widely.",  # the caption itself
        "As Fig. 7 shows, some datasets need hundreds of examples (see also Figure 70).",
    ],
    ["We return to the efficiency question (Figure 7) in Section 6."],
]


def test_a_figure_gets_the_sentences_that_cite_it() -> None:
    context = figure_context.figure_context(PAPER, "Figure 7: The data efficiency of zero-shot transfer", 1)
    assert context.startswith("As Fig. 7 shows")  # its own page first; the caption line doesn't count
    assert "In Figure 7, we show how many labeled examples per class" in context
    assert "(Figure 7) in Section 6." in context and "Other results follow" not in context
    assert figure_context.figure_context(PAPER, "A chart of efficiency", 1) == ""  # no number, no context
    assert figure_context.figure_context(PAPER, "Figure 3: Something else", 1) == ""


def test_a_photo_is_described_only_with_a_caption_or_a_citation() -> None:
    photo = SeenFigure((100, 100, 400, 300), "photo", True)
    assert not vision.describable(photo, [], PAGE_AREA)
    assert vision.describable(replace(photo, caption="Figure 1: The ENIAC programmers"), [], PAGE_AREA)
    assert vision.describable(photo, [], PAGE_AREA, context="As Figure 1 shows, they worked in pairs.")
    assert not vision.describable(replace(photo, type="logo", caption="Figure 1: Our logo"), [], PAGE_AREA)


def test_the_context_goes_into_the_prompt_and_the_cache_name(tmp_path: Path, openai_fake: FakeOpenAI) -> None:
    openai_fake.reply(json=chat_reply({"useful": True, "description": DESCRIPTION, "key_values": []}))
    data, box = make_figure_pdf()
    doc = pymupdf.open(stream=data, filetype="pdf")
    doc[0].insert_text((60, 800), "The encoder and decoder of Figure 1 each stack six layers.", fontsize=11)
    path = write(tmp_path, doc.tobytes())
    cache = tmp_path / "cache"
    entry = vision_calls.cache_file(cache, "page-model", page_key0(path))
    entry.parent.mkdir(parents=True)
    entry.write_text(json.dumps(figure_answer(box)), encoding="utf-8")
    asyncio.run(
        vision.see_pages(
            path, model="page-model", cache_dir=cache, max_pages=500, figure_model="figure-model"
        )
    )
    [request] = openai_fake.requests
    prompt = request["messages"][0]["content"][0]["text"]
    assert "<context>The encoder and decoder of Figure 1 each stack six layers.</context>" in prompt
    assert list(cache.glob(f"figure-model/*-f0-d{prompts.DESCRIBE_PROMPT_VERSION}-*.json"))
