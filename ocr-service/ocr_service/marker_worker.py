"""
What runs inside each worker process: Marker with its models attached to the
shared Surya server, driven through the removal pipeline.

Marker is imported inside the functions so the HTTP process stays light.
"""

import base64
import io
import math
import os

from . import pipeline

_models: dict | None = None


def budget_inference(workers: int) -> None:
    """
    Split the Surya server's capacity across the worker pool, as Marker's own
    batch converter does: each client otherwise fans out to the full capacity,
    so N workers would queue N times what the server can run.
    """
    if "SURYA_INFERENCE_PARALLEL" in os.environ:
        return
    from surya.inference import SuryaInferenceManager

    capacity = SuryaInferenceManager(lazy=False).capacity()
    os.environ["SURYA_INFERENCE_PARALLEL"] = str(max(1, math.ceil(capacity * 1.5 / workers)))


def init() -> None:
    global _models
    from marker.models import create_model_dict

    _models = create_model_dict()


def processors() -> list[str]:
    """Marker's defaults, minus MarginaliaProcessor, plus the image-only re-OCR.

    MarginaliaProcessor hides short text in the top 8% of a page as a running
    header; on legal-size pages that reaches the document title.
    """
    from marker.converters.pdf import PdfConverter
    from marker.processors.marginalia import MarginaliaProcessor
    from marker.util import classes_to_strings

    from .processors import ImageOnlyReOCRProcessor

    kept = [p for p in PdfConverter.default_processors if p is not MarginaliaProcessor]
    return classes_to_strings([ImageOnlyReOCRProcessor, *kept])


def empty_blocks(document) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Every non-Picture block Surya could only read as a bare image."""
    from marker.schema import BlockTypes

    from .processors import is_image_only, leaf_blocks

    return [
        (idx, tuple(block.polygon.bbox))
        for idx, page in enumerate(document.pages)
        for block in leaf_blocks(document, page)
        if block.block_type != BlockTypes.Picture and is_image_only(block)
    ]


def convert(pdf: bytes, options: dict) -> dict:
    from marker.config.parser import ConfigParser
    from marker.converters.pdf import PdfConverter
    from marker.output import text_from_rendered
    from marker.settings import settings

    pages, numbers = _render(pdf, options.get("page_range"))

    # The page range is applied when rendering, so Marker sees only those pages.
    parser = ConfigParser({**options, "page_range": None})
    config = parser.generate_config_dict()
    config["pdftext_workers"] = 1
    renderer = parser.get_renderer()
    processor_list = processors()

    def marker_pass(document_pdf: bytes) -> pipeline.MarkerPass:
        converter = PdfConverter(
            config=dict(config),
            artifact_dict=_models,
            processor_list=processor_list,
            renderer=renderer,
        )
        with converter.filepath_to_str(io.BytesIO(document_pdf)) as path:
            document = converter.build_document(path)
            rendered = converter.resolve_dependencies(converter.renderer)(document)
        return pipeline.MarkerPass(rendered=rendered, empty_blocks=empty_blocks(document))

    result = pipeline.run(pages, marker_pass, numbers)

    text, _, images = text_from_rendered(result.rendered)
    encoded = {}
    for name, image in images.items():
        buffer = io.BytesIO()
        image.save(buffer, format=settings.OUTPUT_IMAGE_FORMAT)
        encoded[name] = base64.b64encode(buffer.getvalue()).decode(settings.OUTPUT_ENCODING)

    metadata = dict(getattr(result.rendered, "metadata", None) or {})
    return pipeline.build_response(
        options["output_format"], text, encoded, metadata, result.removed
    )


def _render(pdf: bytes, page_range: str | None):
    import pypdfium2
    from marker.util import parse_range_str

    document = pypdfium2.PdfDocument(pdf)
    try:
        numbers = parse_range_str(page_range) if page_range else list(range(len(document)))
        pages = [
            document[n].render(scale=pipeline.DPI / 72).to_pil().convert("RGB") for n in numbers
        ]
    finally:
        document.close()
    return pages, numbers
