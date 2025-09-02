from datetime import datetime
from typing import Dict, List, Optional, Tuple, cast
from tqdm import tqdm
import notional
from notional.blocks import Paragraph, TextObject, Quote, Page
from notional.query import TextCondition
from notional.types import Date, ExternalFile, Number, RichText, Title, Checkbox
from requests import get

# from notional.text import Annotations

# from more_itertools import grouper


NO_COVER_IMG = "https://via.placeholder.com/150x200?text=No%20Cover"


def export_to_notion(
    all_books: Dict,
    enable_location: bool,
    enable_highlight_date: bool,
    enable_book_cover: bool,
    separate_blocks: bool,
    notion_api_auth_token: str,
    notion_database_id: str,
) -> None:
    print("Initiating transfer...\n")

    for title in all_books:
        each_book = all_books[title]
        author = each_book["author"]
        clippings = each_book["highlights"]
        clippings_count = len(clippings)
        (
            formatted_clippings,
            last_date,
        ) = _prepare_aggregated_text_for_one_book(
            clippings, enable_location, enable_highlight_date
        )
        try:
            message = _add_book_to_notion(
                title,
                author,
                clippings_count,
                formatted_clippings,
                last_date,
                notion_api_auth_token,
                notion_database_id,
                enable_book_cover,
                separate_blocks,
                enable_location,
                enable_highlight_date,
            )
            if message:
                print("✓", message)
            else:
                print("None to add!")
        except Exception as e:
            print(f"An error occured in writing: {title} ({author})")
            raise e


def _prepare_aggregated_text_for_one_book(
    clippings: List, enable_location: bool, enable_highlight_date: bool
) -> Tuple[str, str]:
    # TODO: Special case for books with len(clippings) >= 100 characters. Character limit in a Paragraph block in Notion is 100
    # TODO: last date should be based on sorted clippings, not just the last one in the list.
    formatted_clippings = []
    for each_clipping in clippings:
        aggregated_text = ""
        text = each_clipping[0]
        page = each_clipping[1]
        location = each_clipping[2]
        date = each_clipping[3]
        is_note = each_clipping[4]
        if is_note == True:
            aggregated_text += "> " + "NOTE: \n"

        aggregated_text += text + "\n"
        if enable_location:
            if page != "":
                aggregated_text += "Page: " + page + ", "
            if location != "":
                aggregated_text += "Location: " + location
        if enable_highlight_date and (date != ""):
            aggregated_text += ", Date Added: " + date

        aggregated_text = aggregated_text.strip() + "\n\n"
        formatted_clippings.append(aggregated_text)
        last_date = date
    return formatted_clippings, last_date


def _write_to_page(
    notion: notional.session.Session,
    page_block: Page,
    separate_blocks: bool,
    formatted_clippings,
):
    if separate_blocks:
        for st in tqdm(range(0, len(formatted_clippings), 100)):
            page_contents = [
                Quote[clip.strip()] for clip in formatted_clippings[st : st + 100]
            ]
            notion.blocks.children.append(page_block, *page_contents)
    else:
        page_content = Paragraph["".join(formatted_clippings)]
        notion.blocks.children.append(page_block, page_content)


def _add_book_to_notion(
    title: str,
    author: str,
    clippings_count: int,
    formatted_clippings: list,
    last_date: str,
    notion_api_auth_token: str,
    notion_database_id: str,
    enable_book_cover: bool,
    separate_blocks: bool,
    enable_location: bool,
    enable_highlight_date: bool,
) -> Optional[str]:
    notion = notional.connect(auth=notion_api_auth_token)
    last_date_dt = datetime.strptime(last_date, "%A, %d %B %Y %I:%M:%S %p")

    query = (
        notion.databases.query(notion_database_id)
        .filter(property="Title", rich_text=TextCondition(equals=title))
        .limit(1)
    )
    page_block: Page = cast(Page, query.first())
    needs_writing: bool = False

    title_and_author = title + " (" + str(author) + ")"
    print(title_and_author)
    print("-" * len(title_and_author))

    if page_block:
        last_highlighted_dt = cast(
            datetime,
            page_block.properties["Last Highlighted"].date.start or datetime.min,
        )
        blockquoted = cast(bool, page_block.properties["Blockquoted"].checkbox)
        includes_location = cast(
            bool, page_block.properties["Includes Location"].checkbox
        )
        includes_timestamp = cast(
            bool, page_block.properties["Includes Timestamp"].checkbox
        )
        current_highlight_count = page_block.properties["Highlights"].number or 0

        needs_writing = (
            (
                last_highlighted_dt.replace(second=0, tzinfo=None)
                < last_date_dt.replace(second=0, tzinfo=None)
            )
            | (includes_location ^ enable_location)
            | (includes_timestamp ^ enable_highlight_date)
            | (blockquoted ^ separate_blocks)
            | (current_highlight_count != len(formatted_clippings))
        )

    else:
        needs_writing = True

    if not needs_writing:
        return

    # Clear the contents of the existing page if we are rewriting.
    if page_block:
        notion.pages.delete(page_block)

    # Create a brand new page block with the correct properties
    page_block = notion.pages.create(
        parent=notion.databases.retrieve(notion_database_id),
        properties={
            "Title": Title[title],
            "Author": RichText[author],
            "Last Highlighted": Date[last_date_dt.isoformat()],
            "Blockquoted": Checkbox[separate_blocks],
            "Includes Location": Checkbox[enable_location],
            "Includes Timestamp": Checkbox[enable_highlight_date],
        },
        children=[],
    )

    if enable_book_cover:
        # Fetch a book cover from Google Books if the cover for the page is not set
        if page_block.cover is None:
            result = _get_book_cover_uri(title, author)

        if result is None:
            # Set the page cover to a placeholder image
            cover = ExternalFile[NO_COVER_IMG]
            print(
                "× Book cover couldn't be found. "
                "Please replace the placeholder image with the original book cover manually."
            )
        else:
            # Set the page cover to that of the book
            cover = ExternalFile[result]
            print("✓ Added book cover.")

        notion.pages.set(page_block, cover=cover)

    _write_to_page(
        notion=notion,
        page_block=page_block,
        separate_blocks=separate_blocks,
        formatted_clippings=formatted_clippings,
    )

    # Only write this once content has been succesfully written to page
    notion.pages.update(
        page_block,
        **{
            "Highlights": Number[clippings_count],
            "Last Synced": Date[datetime.now().isoformat()],
        },
    )
    page_block.properties["Highlights"] = Number[clippings_count]
    message = str(clippings_count) + " notes/highlights added successfully.\n"

    return message


# def _create_rich_text_object(text):
#     if "Note: " in text:
#         # Bold text
#         nested = TextObject._NestedData(content=text)
#         rich = TextObject(text=nested, plain_text=text, annotations=Annotations(bold=True))
#     elif any(item in text for item in ["Page: ", "Location: ", "Date Added: "]):
#         # Italic text
#         nested = TextObject._NestedData(content=text)
#         rich = TextObject(text=nested, plain_text=text, annotations=Annotations(italic=True))
#     else:
#         # Plain text
#         nested = TextObject._NestedData(content=text)
#         rich = TextObject(text=nested, plain_text=text)
#     return rich


# def _update_book_with_clippings(formatted_clippings):
#     rtf = []
#     for each_clipping in formatted_clippings:
#         each_clipping_list = each_clipping.split("*")
#         each_clipping_list = list(filter(None, each_clipping_list))
#         for each_line in each_clipping_list:
#             rtf.append(_create_rich_text_object(each_line))
#     print(len(rtf))
#     content = Paragraph._NestedData(rich_text=rtf)
#     para = Paragraph(paragraph=content)
#     return para


def _get_book_cover_uri(title: str, author: str):
    req_uri = "https://www.googleapis.com/books/v1/volumes?q="

    if title is None:
        return
    req_uri += "intitle:" + title

    if author is not None:
        req_uri += "+inauthor:" + author

    response = get(req_uri).json().get("items", [])
    if len(response) > 0:
        for x in response:
            if x.get("volumeInfo", {}).get("imageLinks", {}).get("thumbnail"):
                return (
                    x.get("volumeInfo", {})
                    .get("imageLinks", {})
                    .get("thumbnail")
                    .replace("http://", "https://")
                )
    return
