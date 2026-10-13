"""Heading detection and section paths. No PDF, no DB, no OpenAI."""

from ingest.headings import outline
from ingest.parse import Block, body_size

BODY = "Chlorophyll absorbs red and blue light and reflects green, which is why leaves look green."


def b(text: str, size: float = 10, bold: bool = False) -> Block:
    return Block(text, size, bold, 100, 110)


def paths(pages: list[list[Block]]) -> list[tuple[str, tuple[str, ...], int | None]]:
    return [(p.text[:12], p.path, p.level) for p in outline(pages)]


def test_body_size_is_the_char_weighted_median() -> None:
    assert body_size([[b("Big title", 24), b(BODY), b(BODY, 9)], [b(BODY)]]) == 10
    assert body_size([]) == 0


def test_levels_follow_sizes_and_paths_nest() -> None:
    pages = [
        [b("Chapter 3", 18), b("Photosynthesis", 14), b("Light reactions", 10, bold=True), b(BODY)],
        [
            b(BODY),
            b("Calvin cycle", 10, bold=True),
            b(BODY),
            b("Respiration", 14),
            b(BODY),
            b("Chapter 4", 18),
            b(BODY),
        ],
    ]
    ch3, photo = ("Chapter 3",), ("Chapter 3", "Photosynthesis")
    assert paths(pages) == [
        ("Chapter 3", ch3, 0),
        ("Photosynthes", photo, 1),
        ("Light reacti", (*photo, "Light reactions"), 2),
        (BODY[:12], (*photo, "Light reactions"), None),
        (BODY[:12], (*photo, "Light reactions"), None),
        ("Calvin cycle", (*photo, "Calvin cycle"), 2),
        (BODY[:12], (*photo, "Calvin cycle"), None),
        ("Respiration", ("Chapter 3", "Respiration"), 1),
        (BODY[:12], ("Chapter 3", "Respiration"), None),
        ("Chapter 4", ("Chapter 4",), 0),
        (BODY[:12], ("Chapter 4",), None),
    ]
    assert [p.page for p in outline(pages)][3:5] == [1, 2]


def test_not_headings() -> None:
    pages = [
        [
            b("A big-font paragraph. " * 10, 18),  # too long
            b("Plants need light.", 14),  # ends like a sentence
            b("Slightly larger", 11),  # under the size ratio
            b("Small print", 7, bold=True),  # bold but smaller than body
            *[b(BODY)] * 3,
        ]
    ]
    assert all(p.level is None and p.path == () for p in outline(pages))


def test_bold_body_text_does_not_make_every_line_a_heading() -> None:
    pages = [[b("Section A", 14), *[b("Short bold line", bold=True), b(BODY, bold=True)] * 3]]
    levels = [p.level for p in outline(pages)]
    assert levels == [0] + [None] * 6


def test_more_sizes_than_levels_merge_into_the_last() -> None:
    sizes = [30, 24, 20, 16, 13]
    pages = [[*(b(f"Heading {s}", s) for s in sizes), *[b(BODY)] * 6]]
    assert [p.level for p in outline(pages)[:5]] == [0, 1, 2, 3, 3]
    assert outline(pages)[-1].path == ("Heading 30", "Heading 24", "Heading 20", "Heading 13")


def test_no_headings_no_paths() -> None:
    assert all(p.path == () and p.level is None for p in outline([[b(BODY)] * 3, [b(BODY)]]))
    assert outline([]) == []


SLIDE_BODY = "Write tests even before writing the code"


def deck(slides: int = 4) -> list[list[Block]]:
    """A slide deck: a 44pt title page, then 32pt slide titles, 28pt bullets and 24pt sub-bullets."""
    title = [b("Collaborative Software Development", 44), b("Software Testing", 32), b(BODY, 24)]
    return [title] + [
        [b(f"Slide {n}", 32), b("▪Main point", 28), *[b("▪" + SLIDE_BODY, 24)] * 4]
        for n in range(2, slides + 1)
    ]


def test_bullets_are_never_headings() -> None:
    levels = {p.text: p.level for p in outline(deck())}
    assert levels["▪Main point"] is None and levels["Slide 2"] == 0


def test_a_one_page_title_size_is_not_a_section() -> None:
    paras = outline(deck())
    assert paras[0].level is None and paras[0].path == ()  # the 44pt course title
    assert [p.text for p in paras if p.level == 0] == ["Software Testing", "Slide 2", "Slide 3", "Slide 4"]


def test_short_documents_keep_their_one_page_headings() -> None:
    pages = [[b("Handout", 20), b("Branches", 14), b(BODY), b("Reviews", 14), b(BODY)]]
    assert [p.level for p in outline(pages) if p.level is not None] == [0, 1, 1]


def test_wrapped_lines_are_joined_and_never_headings() -> None:
    pages = deck()
    pages[1][1:2] = [b("▪Write tests help understand the", 28), b("specification and business problem", 28)]
    texts = [(p.text, p.level) for p in outline(pages) if p.page == 2]
    assert texts[1] == ("▪Write tests help understand the specification and business problem", None)


def test_a_line_ending_a_sentence_is_not_continued() -> None:
    pages = [[b("First line ends here.", 10), b("lower-case start", 10), b(BODY)]]
    assert [p.text for p in outline(pages)][:2] == ["First line ends here.", "lower-case start"]


def test_page_numbers_opening_or_closing_a_page_are_dropped() -> None:
    pages = deck()
    pages[1] = [b("2", 12), *pages[1], b("12", 12)]
    pages[2] = [*pages[2][:2], b("42", 24), *pages[2][2:]]  # mid-page: a number in the content stays
    texts = [p.text for p in outline(pages)]
    assert "2" not in texts and "12" not in texts and "42" in texts


def report(extra: dict[int, list[Block]] | None = None, pages: int = 6) -> list[list[Block]]:
    """A report: 25pt cover title on pages 1-2, 17pt chapters and 14pt sections on later pages."""
    doc = [[b("AI Risk Management", 25), b("Framework (AI RMF 1.0)", 25)] for _ in range(2)]
    doc += [
        [b(f"{n}. Chapter {n}", 17), b(BODY), b(f"{n}.1 Section", 14), b(BODY)] for n in range(1, pages - 1)
    ]
    for page_no, blocks in (extra or {}).items():
        doc[page_no - 1] = blocks
    return doc


def test_a_title_on_the_first_two_pages_is_not_a_section() -> None:
    paras = outline(report())
    assert all(p.level is None for p in paras if p.page <= 2)
    assert [p.text for p in paras if p.level == 0][:2] == ["1. Chapter 1", "2. Chapter 2"]


def test_a_size_used_once_is_not_a_section() -> None:
    pages = report()
    pages[3].insert(0, b("How to Use This Report", 30))  # a one-off banner, bigger than the chapters
    paras = outline(pages)
    assert next(p for p in paras if p.text.startswith("How to")).level is None
    assert next(p for p in paras if p.text == "2. Chapter 2").path == ("2. Chapter 2",)


def test_a_heading_wrapped_over_two_lines_is_one_heading() -> None:
    pages = report()
    pages[2][0:1] = [b("1. Understanding and Addressing Risks", 17), b("Impacts and Harms", 17)]
    paras = outline(pages)
    assert (
        paras[[p.text for p in paras].index(BODY, 4)].path[0]
        == "1. Understanding and Addressing Risks Impacts and Harms"
    )


def test_numbered_headings_in_a_row_stay_separate() -> None:
    pages = report()
    pages[2][0:2] = [b("1. Chapter 1", 17), b("2. Chapter 2", 17)]
    assert [p.text for p in outline(pages) if p.level == 0][:2] == ["1. Chapter 1", "2. Chapter 2"]


def test_contents_pages_are_dropped() -> None:
    toc = [b("Table of Contents", 17), b("1. Chapter 1 3"), b("1.1 Section 3"), b("2. Chapter 2 4")]
    more = [b("2.1 Section 4"), b("3. Chapter 3 5"), b("Appendix A 9")]
    pages = report({1: [b("Cover text that stays", 10), *toc], 2: more})
    texts = [p.text for p in outline(pages)]
    assert "Cover text that stays" in texts
    assert not any(
        t in texts for t in ["Table of Contents", "1. Chapter 1 3", "2.1 Section 4", "Appendix A 9"]
    )
    assert "1. Chapter 1" in texts  # the real chapter after the contents is untouched


def test_a_contents_slide_without_entries_is_kept() -> None:
    pages = deck()
    pages[1] = [b("Contents", 32), b("▪Why we test"), b("▪How to write a unit test")]
    assert "▪Why we test" in [p.text for p in outline(pages)]


# ---- the PDF outline


def methods_report() -> tuple[list[list[Block]], list[tuple[int, str, int]]]:
    """Subsections and sub-subsections in the same size (NIST-style), a 20pt banner the outline doesn't list,
    and an outline with three levels."""
    pages = [
        [
            b("1. Methods", 14),
            b(BODY),
            b("1.1 Sampling", 12, True),
            b(BODY),
            b("1.1.1 Field work", 12, True),
            b(BODY),
        ],
        [b("Key findings", 20), b(BODY), b("1.2 Analysis", 12, True), b(BODY), b("Aside", 10, True), b(BODY)],
        [
            b("2. Results", 14),
            b(BODY),
            b("2.1 Tables", 12, True),
            b(BODY),
            b("Small note", 10, True),
            b(BODY),
        ],
    ]
    toc = [
        (1, "Methods", 1),
        (2, "Sampling", 1),
        (3, "Field work", 1),
        (2, "Analysis", 2),
        (1, "Results", 3),
        (2, "Tables", 3),
    ]
    return pages, toc


def test_outline_levels_win_over_sizes() -> None:
    pages, toc = methods_report()
    levels = {p.text: p.level for p in outline(pages, toc=toc) if p.level is not None}
    assert levels == {
        "1. Methods": 0,
        "1.1 Sampling": 1,
        "1.1.1 Field work": 2,
        "1.2 Analysis": 1,
        "2. Results": 0,
        "2.1 Tables": 1,
        "Aside": 3,
        "Small note": 3,
    }  # smaller unlisted headings sit below it
    by_size = {p.text: p.level for p in outline(pages) if p.level is not None}
    assert by_size["1.1 Sampling"] == by_size["1.1.1 Field work"]  # what the sizes alone give


def test_a_banner_the_outline_does_not_list_is_body_text() -> None:
    pages, toc = methods_report()
    [banner] = [p for p in outline(pages, toc=toc) if p.text == "Key findings"]
    assert banner.level is None and banner.path == ("1. Methods", "1.1 Sampling", "1.1.1 Field work")


def test_an_outline_mostly_not_found_is_ignored() -> None:
    pages, _ = methods_report()
    toc = [(1, "Introduction", 1), (2, "Scope", 1), (1, "Discussion", 2), (2, "Limits", 3)]
    assert outline(pages, toc=toc) == outline(pages)


def test_a_flat_outline_is_ignored() -> None:
    assert outline(deck(), toc=[(1, "Software Testing", 1), (1, "Slide 2", 2), (1, "Slide 3", 3)]) == outline(
        deck()
    )


def test_an_outline_entry_on_the_wrong_page_is_still_found() -> None:
    pages, toc = methods_report()
    toc[0] = (1, "Methods", 1)
    toc[4] = (1, "Results", 1)  # NIST's first entries all point at page 1
    assert {p.text: p.level for p in outline(pages, toc=toc)}["2. Results"] == 0


def test_a_wrapped_listed_heading_is_joined() -> None:
    pages, toc = methods_report()
    pages[0][2:3] = [b("1.1 Sampling of the", 12, True), b("population", 12, True)]
    toc[1] = (2, "Sampling of the population", 1)
    paras = outline(pages, toc=toc)
    assert next(p.text for p in paras if p.level == 1) == "1.1 Sampling of the population"
