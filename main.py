"""
This Add-On uses Azure's Document Intelligence API
to perform OCR on documents within DocumentCloud
"""

import os
import re
import sys
import time

from azure.ai.formrecognizer import DocumentAnalysisClient
from azure.core.credentials import AzureKeyCredential
from documentcloud.addon import AddOn
from documentcloud.exceptions import APIError


def _convert_coordinates(polygon, page_width, page_height):
    """Converts Azure's absolute coordinates to relative
    page coordinates used by DocumentCloud
    """
    x_values = [point.x for point in polygon]
    y_values = [point.y for point in polygon]

    x1 = min(x_values)
    x2 = max(x_values)
    y1 = min(y_values)
    y2 = max(y_values)

    return (
        max(0, min(1, x1 / page_width)),
        max(0, min(1, x2 / page_width)),
        max(0, min(1, y1 / page_height)),
        max(0, min(1, y2 / page_height)),
    )


def build_page(index, page):
    """Convert an Azure result page into a DocumentCloud page dict."""
    text = "\n".join(
        "" if re.match(r"^[:.\-]*$", line.content.strip()) else line.content
        for line in page.lines
    )
    positions = []
    for word in page.words:
        x1, x2, y1, y2 = _convert_coordinates(word.polygon, page.width, page.height)
        positions.append(
            {
                "text": word.content,
                "x1": x1,
                "x2": x2,
                "y1": y1,
                "y2": y2,
            }
        )
    return {
        "page_number": index,
        "text": text,
        "ocr": "azuredi",
        "positions": positions,
    }


class DocumentIntelligence(AddOn):
    """Class for Document Intelligence Add-On"""

    def validate(self):
        """Validate that we can run the OCR"""
        if self.get_document_count() is None:
            self.set_message(
                "It looks like no documents were selected. Search for some or "
                "select them and run again."
            )
            sys.exit(0)
        num_pages = 0
        for document in self.get_documents():
            num_pages += document.page_count
        try:
            self.charge_credits(num_pages)
        except ValueError:
            return False
        except APIError:
            return False
        return True

    def upload_pages(self, document, pages, max_retries=5, retry_delay=60):
        """PATCH pages to the API in chunks, retrying while still processing."""
        page_chunk_size = 20
        for i in range(0, len(pages), page_chunk_size):
            chunk = pages[i : i + page_chunk_size]
            retries = 0
            while retries < max_retries:
                print(f"Updating the page text (pages {i} to {i + page_chunk_size})")
                try:
                    resp = self.client.patch(
                        f"documents/{document.id}/", json={"pages": chunk}
                    )
                    resp.raise_for_status()
                except APIError as exc:
                    if "processing" in str(exc):
                        print(
                            "Document is still processing, retrying... "
                            f"(Attempt {retries + 1} of {max_retries})"
                        )
                        retries += 1
                        time.sleep(retry_delay)
                        continue
                    print(f"Unexpected error: {exc}. Exiting retries.")
                    raise
                print("Completed updating the page text")
                break
            else:
                print(
                    f"Failed to update pages {i} to {i + page_chunk_size}"
                    f" after {max_retries} attempts."
                )
                self.set_message(
                    "Failed to update page text in a timely manner. "
                    "Please email info@documentcloud.org to debug."
                )
                sys.exit(1)

    def tag_document(self, document, max_retries=5, retry_delay=60):
        """Tag the document with the OCR engine, retrying on API errors."""
        retries = 0
        while retries < max_retries:
            try:
                print("Tagging document...")
                existing = document.data.get("ocr_engine", [])
                self.client.patch(
                    f"documents/{document.id}/data/ocr_engine/",
                    json={"values": ["azure"], "remove": existing},
                )
                self.client.patch(
                    f"documents/{document.id}/data/ocr_engine/",
                    json={"values": ["azure"]},
                )
                print("Finished tagging document")
                break
            except APIError as exc:
                print(f"Error tagging document. {exc}. Retrying...")
                retries += 1
                time.sleep(retry_delay)
        else:
            print(f"Failed to tag document after {max_retries} attempts.")
            self.set_message(
                "Failed to set the OCR tag for this document. "
                "Email info@documentcloud.org to debug."
            )
            sys.exit(1)

    def main(self):
        """The main add-on functionality goes here."""
        self.client.session.headers.update({"User-Agent": "Azure OCR Add-On"})
        if not self.validate():
            self.set_message("You do not have sufficient AI credits to run this Add-On")
            sys.exit(0)
        key = os.environ.get("KEY")
        endpoint = os.environ.get("TOKEN")
        document_analysis_client = DocumentAnalysisClient(
            endpoint=endpoint, credential=AzureKeyCredential(key)
        )
        to_tag = self.data.get("to_tag", False)
        for document in self.get_documents():
            poller = document_analysis_client.begin_analyze_document(
                "prebuilt-read", document=document.pdf
            )
            result = poller.result()
            pages = [build_page(i, page) for i, page in enumerate(result.pages)]
            self.upload_pages(document, pages)
            if to_tag:
                self.tag_document(document)


if __name__ == "__main__":
    DocumentIntelligence().main()
