"""Architecture diagrams via PaperBanana. Never data charts.

The division is not stylistic. `discovery_vs_final`, `recall_vs_tier` and
`latency_cost` plot *measured numbers*; a generative image model producing bars
whose heights are not derived from the data is a correctness hazard in a
benchmark report, and it is not reproducible between runs. Those stay in
matplotlib (`scoring/report.py:write_plots`). PaperBanana draws the things it is
actually for — architecture and methodology — where there is no axis to get
wrong.

Off by default. Generation is slow, paid, and non-deterministic, and an
architecture diagram does not change between runs of the same pipeline. The
report embeds whatever is already in `experiments/{id}/figures/`, so a default
run reuses the last set rather than showing nothing.

Authentication is the awkward part. PaperBanana supports only the Gemini
Developer API via ``GOOGLE_API_KEY`` — there is no Vertex or ADC path — while
this project runs on Vertex and the runner image sets
``GOOGLE_GENAI_USE_VERTEXAI=true`` globally. So the key comes from Secret
Manager at runtime (never a task env var, which would bake it into the compiled
spec and the PipelineJob resource) and the Vertex flag is unset for this call
only.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

# parents[3], not three `.parent` hops: scoring -> bq_context -> src -> root.
# `parent.parent.parent` lands in `src/`, where there is no .env, so the load is
# a silent no-op — the same latent bug as in the vendored corpus scripts.
# override=False so an exported variable, or the container's task environment,
# always wins over the file. A missing .env is a no-op, which is the normal case
# in the runner image.
load_dotenv(Path(__file__).resolve().parents[3] / ".env", override=False)

logger = logging.getLogger(__name__)


def secret_id() -> str:
    """Which Secret Manager secret to read the API key from.

    Read at call time rather than import time so `.env` and the environment can
    change it without the module having to be reimported — and so tests can set
    it with `monkeypatch.setenv` rather than reaching into module state.

    Sourced from `SECRET_ID`, which `.env` carries locally and the CLI loads on
    startup. In the pipeline there is no `.env`; the value comes from the task
    environment, or falls back to the default.
    """
    import os  # noqa: PLC0415

    name = os.getenv("SECRET_ID", "").strip()
    if not name:
        # Deliberately no fallback. A default would be a name invented here that
        # nobody configured, and reading the wrong secret is worse than saying so.
        message = (
            "SECRET_ID is unset or empty. Set it in .env for local runs, or in the "
            "task environment for the pipeline; it names the Secret Manager secret "
            "holding a Gemini Developer API key."
        )
        raise RuntimeError(message)
    return name


#: House style, matching the diagrams already in docs/images/.
STYLE = (
    "Professional, clean architecture diagram in the style of official Google Cloud "
    "Platform documentation. GCP brand colors: blue (#4285F4), green (#34A853), "
    "yellow (#FBBC05), red (#EA4335). Clean white background. Google Cloud product "
    "icon style, clean lines, no 3D effects, no hexagons, modern flat design."
)

#: What we ask for. Architecture and methodology only — nothing with an axis.
DIAGRAMS: dict[str, str] = {
    "experiment_design": (
        "Factorial experiment design as three titled horizontal bands stacked top to "
        "bottom. Band 1 'Corpus': four enrichment tiers, each a rounded rectangle, "
        "labelled tier 0 schema only, tier 1 plus profiling, tier 2 plus glossary, "
        "tier 3 plus table aspect. Band 2 'Approaches': six rounded rectangles in a "
        "row labelled bq_tools, kc_search, kc_context, context_prefilter, "
        "semantic_context, search_direct. Band 3 'Measurement': one box '25 questions "
        "x 5 runs' flowing left to right into 'graded relevance scoring' into "
        "'recall, nDCG@5, precision'. There are NO arrows between bands."
    ),
}


def api_key(project: str) -> str | None:
    """Fetch the Developer API key from Secret Manager, or None if unavailable.

    None rather than an exception: figure generation is an optional extra inside
    the exit task, and the exit task is the only thing allowed to turn a run red
    — which it must do only for missing cells.
    """
    # Resolved before the try: calling secret_id() inside the handler would
    # re-raise when an unset SECRET_ID is the very thing that failed.
    try:
        configured = secret_id()
    except RuntimeError as exc:
        logger.warning("%s Skipping figures.", exc)
        return None

    try:
        from google.cloud import secretmanager  # noqa: PLC0415

        client = secretmanager.SecretManagerServiceClient()
        name = f"projects/{project}/secrets/{configured}/versions/latest"
        return client.access_secret_version(request={"name": name}).payload.data.decode().strip()
    except Exception as exc:  # noqa: BLE001 - absent secret and denied access are both "skip"
        logger.warning(
            "No Gemini Developer API key in secret %r (%s): skipping figures",
            configured,
            type(exc).__name__,
        )
        return None


#: A renderer: (source_context, intent) -> path of the produced image, or None.
#: Injected as a plain callable rather than a PaperBanana object so the
#: orchestration below is testable without the optional dependency installed.
Renderer = Callable[[str, str], "Path | None"]


def _paperbanana_renderer(key: str) -> Renderer | None:  # pragma: no cover - needs the extra
    """Build a renderer backed by PaperBanana, or None if it is not installed."""
    try:
        # Lazy by necessity: the extra may be absent. ty excludes this module
        # for the same reason (see pyproject).
        from paperbanana import DiagramType, GenerationInput, PaperBananaPipeline  # noqa: PLC0415
        from paperbanana.core.config import Settings  # noqa: PLC0415
    except ImportError:
        logger.warning("paperbanana is not installed; skipping figures")
        return None

    # Scoped to this process. The image sets GOOGLE_GENAI_USE_VERTEXAI=true, and
    # google-genai honours that over GOOGLE_API_KEY — which surfaces as a
    # confusing "GOOGLE_API_KEY not found" rather than an auth error.
    os.environ["GOOGLE_API_KEY"] = key
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "0"
    pipeline = PaperBananaPipeline(
        settings=Settings(vlm_provider="google", image_provider="google")
    )

    def render(source_context: str, intent: str) -> Path | None:
        import asyncio  # noqa: PLC0415
        from pathlib import Path as _Path  # noqa: PLC0415

        result = asyncio.run(
            pipeline.generate(
                GenerationInput(
                    source_context=source_context,
                    communicative_intent=intent,
                    diagram_type=DiagramType.METHODOLOGY,
                )
            )
        )
        return _Path(result.image_path) if result.image_path else None

    return render


def generate(project: str, out_dir: Path, *, renderer: Renderer | None = None) -> list[Path]:
    """Render the architecture diagrams into ``out_dir``. Returns what was written."""
    import shutil  # noqa: PLC0415

    if renderer is None:
        key = api_key(project)
        if not key:
            return []
        renderer = _paperbanana_renderer(key)
        if renderer is None:
            return []

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, description in DIAGRAMS.items():
        try:
            source = renderer(f"{STYLE}\n\n{description}", name.replace("_", " "))
        except Exception:  # one bad diagram must not lose the rest
            logger.warning("Could not generate %s", name, exc_info=True)
            continue
        if source is None or not source.exists():
            logger.warning("Renderer produced nothing for %s", name)
            continue
        target = out_dir / f"{name}.png"
        shutil.copyfile(source, target)
        written.append(target)
    return written
