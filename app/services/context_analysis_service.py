"""Context Analysis Service — Unified Extraction Call.

Analyzes the entire transcript in a single LLM call to extract topic,
terminology, and a style guide before translation begins. This ensures
consistent, context-aware translations.

Previously a two-pass system (topic/terms, then a near-duplicate
re-extraction pass) with a closed 5-value domain enum that was never
actually reachable from the UI. Now: one call, the model self-identifies
the content's specialized domain from the transcript itself (no fixed
list), and the same call also produces the style guide (tone, formality,
audience) that previously lived in an unused, disconnected code path in
translation_pipeline.py.

Refactored to use short-lived database sessions to prevent SQLite locking.
The provider (Gemini by default, OpenAI for BYOK) comes from app.core.llm.
"""

import json
import re
from datetime import datetime
from typing import Any, cast

from sqlalchemy.orm import Session, make_transient

from app.core.llm import completion_kwargs, resolve_llm_target
from app.db.session import SessionLocal
from app.models.video import Segment, Term, Video, VideoStatus
from app.services.progress_service import get_progress_tracker


def _calculate_term_budget(segment_count: int) -> int:
    """Calculate a dynamic term budget based on transcript length.

    A floor of 10 forced short transcripts (a couple of sentences) to yield
    10 "key terms" regardless of content, which pushed the model into
    labeling ordinary words (e.g. "use", "detect") as glossary terms. A
    lower floor lets very short inputs get a handful of genuine terms
    instead of a quota of invented ones; longer transcripts still scale up
    to 100.
    """
    return min(max(3, segment_count // 3), 100)


# Short-drama genre presets. Each value is injected into the analysis prompt
# so dialogue tone, honorifics, and terminology locking match the genre before
# translation begins. Kept in English (the prompt language) with the canonical
# Chinese genre labels for reference.
GENRE_GUIDANCE: dict[str, str] = {
    "general": (
        "General content — no genre-specific style constraints. Classify the "
        "material and translate it faithfully in its natural register."
    ),
    "romance": (
        "Romantic drama (甜宠/言情). Dialogue is warm, emotionally direct, and "
        "sometimes flirtatious. Preserve romantic tension and emotional beats; "
        "avoid an overly formal or literary register. Lock character names, "
        "pet-names/endearments, and relationship honorifics (e.g. 哥哥/姐姐/亲爱的) "
        "so they translate consistently in every scene."
    ),
    "suspense": (
        "Suspense/thriller (悬疑). Dialogue is clipped, tense, and loaded with "
        "subtext. Preserve ambiguity, foreshadowing, and reveal timing. Lock "
        "character names, place names, and investigation/crime jargon so clues "
        "and callbacks stay consistent."
    ),
    "costume": (
        "Historical/costume drama (古装, incl. wuxia/xianxia). Era-appropriate "
        "register and honorifics are critical — preserve titles like 公子/姑娘/大人/"
        "师父 and in-world terminology. Prefer a slightly literary, period tone; "
        "do not modernize the register."
    ),
    "urban": (
        "Modern urban drama (都市). Colloquial, contemporary dialogue with natural "
        "slang and everyday speech. Lock product/brand names, workplace jargon, "
        "and modern honorifics so they stay idiomatic in the target language."
    ),
    "fantasy": (
        "Fantasy (奇幻). Invented in-world terminology, character/creature names, "
        "and magic-system terms must be locked and kept consistent. Tone may be "
        "epic or intimate by scene; preserve the invented vocabulary exactly."
    ),
    "scifi": (
        "Science fiction (科幻). Technical and invented terminology, organization/"
        "technology names, and speculative concepts must be locked. Preserve the "
        "precise meaning of invented and technical terms."
    ),
}


def build_context_analysis_prompt(
    full_transcript: str,
    target_language: str,
    source_language: str = "",
    term_budget: int = 15,
    genre: str = "general",
) -> str:
    """Build the unified extraction prompt: domain self-classification, topic,
    terminology, characters, and style guide, all in one call.

    Replaces the old two-pass system (a domain-routed Pass 1 through a
    closed 5-value enum that was never reachable from the UI, followed by a
    near-duplicate Pass 2 re-extraction) with a single call. The model
    identifies the content's specialized domain itself — from the
    transcript — rather than being routed through a fixed list, so content
    that doesn't fit any hardcoded category (a classical music analysis, a
    cooking demonstration, a civil engineering lecture) gets genuinely
    domain-appropriate extraction instead of falling back to a generic pass.

    ``genre`` is a short-drama genre preset (see GENRE_GUIDANCE). For drama
    content it additionally extracts a character list — names plus persona and
    relationships — so character names are locked before translation.
    """

    source_lang_clause = (
        f"The transcript is written in {source_language}." if source_language else ""
    )
    genre_guidance = GENRE_GUIDANCE.get(genre, GENRE_GUIDANCE["general"])

    return f"""You are analyzing a video transcript to prepare it for professional translation into {target_language}.

GENRE CONTEXT:
This content has been classified as: {genre_guidance}
Apply this genre's guidance when deciding tone, honorifics, and which terms to lock.

STEP 1 — IDENTIFY THE DOMAIN:
Read the transcript and identify what specialized subject matter it covers, in your own words
(e.g. "classical music theory and composition", "civil engineering", "criminal law procedure",
"cognitive behavioral therapy", "no specialized domain — general content"). Use this classification
to decide what KINDS of terms matter for this specific content — the categories below are examples
of the pattern to follow, not an exhaustive list:
- Political content → political systems, ideologies, governance concepts, political actors
- Medical content → diseases/conditions, procedures and treatments, anatomy, pharmaceuticals
- Psychological content → disorders, therapeutic approaches, cognitive concepts, assessment tools
- Sociological content → social structures, stratification concepts, social processes, methodologies
- Any other specialized domain → identify the equivalent categories yourself (e.g. for classical
  music: musical forms, compositional techniques, instrumentation, historical periods/movements;
  for law: legal doctrines, procedural terms, case citations; for cooking: techniques, equipment,
  ingredient science)

STEP 2 — EXTRACT TERMINOLOGY:
Extract approximately {term_budget} key terms using the domain-appropriate categories from Step 1,
plus named entities (people, organizations, theories, products, places) — all as they appear in the
SOURCE LANGUAGE of the transcript, with their STANDARD {target_language} translations.

STEP 3 — BUILD A STYLE GUIDE:
Determine the tone and register a translator should use: is this educational, entertainment,
technical, or promotional content? What tone and formality would resonate with {target_language}
speakers? Are there cultural considerations for the translation?

FULL TRANSCRIPT:
{full_transcript}

{source_lang_clause}

Respond in JSON format:
{{
  "detected_domain": "<the specialized subject matter you identified in Step 1, in your own words>",
  "main_topic": "Brief description of the main topic (1 sentence)",
  "sub_topics": ["sub-topic 1", "sub-topic 2", "sub-topic 3"],
  "key_terms": [
    {{
      "original": "<term exactly as it appears in the transcript's source language — DO NOT translate>",
      "target_standard": "Standard {target_language} translation",
      "category": "<a category appropriate to the detected domain, e.g. Technical|Proper Noun|Key Concept, or a domain-specific one like Musical Form|Legal Doctrine>",
      "confidence": "high|medium|low"
    }}
  ],
  "named_entities": [
    {{
      "name": "<name exactly as it appears in the transcript's source language — DO NOT translate>",
      "type": "Person|Organization|Theory|Product|Place",
      "target_translation": "{target_language} translation or transliteration"
    }}
  ],
  "characters": [
    {{
      "name": "<character name exactly as it appears in the source language — DO NOT translate>",
      "target_translation": "{target_language} translation or transliteration",
      "persona": "<one-line persona/role, in {target_language}>",
      "gender": "male|female|unknown",
      "relationship": "<role in the story or relationship to the protagonist, in {target_language}>"
    }}
  ],
  "translation_notes": "Any special notes about translation approach for this content",
  "style_guide": {{
    "tone": "<e.g. formal, casual, professional, conversational>",
    "formality_level": <integer 1-5, where 1 is very casual and 5 is very formal>,
    "target_audience": "<description of the intended audience>",
    "style_notes": ["<specific style instruction 1>", "<specific style instruction 2>"],
    "language_considerations": {{"<key point>": "<explanation>"}}
  }}
}}

CRITICAL TARGET LANGUAGE RULE:
You MUST write the narrative fields (main_topic, sub_topics, translation_notes, and every string value
inside style_guide) entirely in {target_language}. Do not write them in English unless English is the
target language.

CRITICAL SOURCE LANGUAGE RULE:
The "original" field inside key_terms and the "name" field inside named_entities MUST contain the exact
term or concept as written in the native source language of the provided transcript. {source_lang_clause}
If the transcript is in English, the original term must be in English. If the transcript is in Farsi, the
original term must be in Farsi. If the transcript is in German, the original term must be in German.
Do NOT translate the original/name fields into the target language.

EXAMPLE — CORRECT (English source, Farsi target):
  {{"original": "machine learning", "target_standard": "یادگیری ماشین"}}
EXAMPLE — WRONG (English source, Farsi target):
  {{"original": "یادگیری ماشین", "target_standard": "یادگیری ماشین"}}

CRITICAL CHARACTER RULE (drama content):
If the transcript is narrative fiction or drama, extract every recurring character into the
"characters" array. The "name" field MUST be in the source language; "target_translation"
is the locked name used in every scene. The "persona", "gender", and "relationship" fields
must be written in {target_language} so the translator can match each character's voice and
honorifics consistently. If the content has no characters (e.g. a lecture), return an empty
array — do not invent characters.

Guidelines:
- Extract approximately {term_budget} key terms - focus on the most important
- For each term, provide the SINGLE best standard {target_language} translation
- Prioritize terms that appear multiple times in the transcript
- Include multi-word concepts (e.g., "cognitive behavioral therapy", not just "therapy")
- If a term has multiple valid translations, choose the most common/academic one
- Do NOT include ordinary, everyday words (common verbs, generic nouns) just to
  reach the target count. If the transcript doesn't contain {term_budget}
  genuine specialized terms, return fewer. A short list of real terminology
  is better than a padded list of common vocabulary."""  # noqa: E501


def analyze_video_context(
    video_id: str,
    db: Session | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """
    Analyze video context, extract terminology, and build a style guide —
    all in a single LLM call (see module docstring).

    This function uses short-lived database sessions to avoid holding locks
    during the long-running LLM API call.

    Phase 1: Fetch video and segments, build transcript
    Phase 2: Send to the LLM for analysis (NO session held)
    Phase 3: Save results to database

    Args:
        video_id: ID of the video to analyze
        db: Deprecated parameter (kept for backward compatibility)
        model_name: Optional model override (default: configured provider's model)

    Returns:
        Context analysis dictionary with topic, terms, entities, and style_guide
    """
    # Initialize progress tracker (uses short-lived sessions internally)
    progress_tracker = get_progress_tracker(video_id, None)

    # ========================================================================
    # PHASE 1: FETCH - Get video and segments with short-lived session
    # ========================================================================
    with SessionLocal() as session:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video:
            raise ValueError(f"Video not found: {video_id}")

        # Check if video is in ERROR status - abort early
        if video.status == VideoStatus.ERROR.value:
            raise RuntimeError(
                f"Video {video_id} is in ERROR status, aborting context analysis"
            )

        # Extract all needed data before session closes
        target_language = video.target_language
        source_language = video.source_language or ""
        genre = video.genre or "general"
        if not target_language:
            raise ValueError("Target language is not set for this video.")

        segments = (
            session.query(Segment)
            .filter(Segment.video_id == video_id)
            .order_by(Segment.sequence_number)
            .all()
        )

        if not segments:
            raise ValueError("No segments found for context analysis")

        # Build full transcript while session is open
        full_transcript = "\n\n".join(
            [f"[{seg.sequence_number}] {seg.original_text}" for seg in segments]
        )

        # Update status
        video.status = VideoStatus.ANALYZING.value
        session.commit()
        make_transient(video)

    progress_tracker.info(
        "CONTEXT_ANALYSIS", "Starting unified context, terminology, and style analysis"
    )
    progress_tracker.start_step(
        "CONTEXT_ANALYSIS",
        f"Analyzing {len(segments)} segments for domain, terminology, and style",
    )

    # Build prompt
    segment_count = len(segments)
    term_budget = _calculate_term_budget(segment_count)

    prompt = build_context_analysis_prompt(
        full_transcript, target_language, source_language, term_budget, genre
    )

    # ========================================================================
    # PHASE 2: ANALYZE - Call the LLM (NO DATABASE SESSION)
    # ========================================================================
    try:
        target = resolve_llm_target()
        client = target.sync_client()
        model_name = model_name or target.model

        progress_tracker.update_progress(
            status=VideoStatus.ANALYZING.value,
            percent=0,
            current_step="Context Analysis",
            step_detail=f"Sending transcript to {target.label} for analysis...",
        )

        print(f"[CONTEXT_ANALYSIS] Starting analysis of {len(segments)} segments")
        start_time = datetime.utcnow()

        response = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            **completion_kwargs(model_name, 16384, temperature=0.1),
        )

        elapsed = (datetime.utcnow() - start_time).total_seconds()

        # Parse response
        response_text = response.choices[0].message.content or ""

        # Extract JSON from response
        json_match = re.search(
            r"```(?:json)?\s*\n?(.*?)\n?```", response_text, re.DOTALL
        )
        if json_match:
            response_text = json_match.group(1)

        response_text = response_text.strip()

        try:
            context_data = cast(dict[str, Any], json.loads(response_text))
        except json.JSONDecodeError as e:
            progress_tracker.error(
                "CONTEXT_ANALYSIS", "Failed to parse JSON response", str(e)
            )
            raise RuntimeError(f"Failed to parse context analysis: {e}") from e

        # Log results
        key_terms_count = len(context_data.get("key_terms", []))
        entities_count = len(context_data.get("named_entities", []))
        detected_domain = context_data.get("detected_domain", "unknown")

        progress_tracker.end_step(
            f"Analysis complete in {elapsed:.1f}s. Domain: {detected_domain}. "
            f"Found {key_terms_count} key terms, {entities_count} named entities."
        )
        progress_tracker.info(
            "CONTEXT_ANALYSIS",
            f"Complete: {context_data.get('main_topic', 'Unknown topic')}",
            f"Domain: {detected_domain}, Terms: {key_terms_count}, "
            f"Entities: {entities_count}",
        )

        print(f"[CONTEXT_ANALYSIS] ✓ Complete in {elapsed:.1f}s")
        print(f"[CONTEXT_ANALYSIS] Domain: {detected_domain}")
        print(f"[CONTEXT_ANALYSIS] Topic: {context_data.get('main_topic', 'N/A')}")
        print(
            f"[CONTEXT_ANALYSIS] Terms: {key_terms_count}, Entities: {entities_count}"
        )

        # ========================================================================
        # PHASE 3: SAVE - Store results with short-lived session
        # ========================================================================
        with SessionLocal() as session:
            video = session.query(Video).filter(Video.id == video_id).first()
            if video:
                video.context_analysis = json.dumps(
                    context_data, ensure_ascii=False, indent=2
                )
                style_guide = context_data.get("style_guide")
                if style_guide:
                    video.style_guide = json.dumps(style_guide, ensure_ascii=False)
                # Skip the old intermediate CONTEXT_READY/GLOSSARY_EXTRACTING
                # statuses — this is now a single call, so there's no
                # meaningful midpoint between them to report.
                video.status = VideoStatus.TERMS_READY.value
                session.commit()
                make_transient(video)

        # Save key terms using bulk insert for efficiency
        _save_context_terms_bulk(
            video_id, context_data.get("key_terms", []), progress_tracker
        )
        # Save extracted characters as "Character" terms so their names are
        # locked during translation (short-drama feature).
        _save_characters_bulk(
            video_id, context_data.get("characters", []), progress_tracker
        )

        return context_data

    except Exception as e:
        error_msg = str(e)
        progress_tracker.error("CONTEXT_ANALYSIS", "Context analysis failed", error_msg)

        # Update error status with short session
        with SessionLocal() as session:
            video = session.query(Video).filter(Video.id == video_id).first()
            if video:
                video.status = VideoStatus.ERROR.value
                video.error_message = f"Context analysis failed: {error_msg}"
                session.commit()
                make_transient(video)

        raise RuntimeError(f"Context analysis failed: {error_msg}") from e


def _save_context_terms_bulk(
    video_id: str, key_terms: list[dict[str, Any]], progress_tracker: Any = None
) -> None:
    """Save extracted context terms to database using bulk insert for efficiency.

    Args:
        video_id: ID of the video
        key_terms: List of term dictionaries from context analysis
        progress_tracker: Optional progress tracker for logging
    """
    if not key_terms:
        return

    terms_to_insert = []

    for term_data in key_terms:
        original = term_data.get("original", "").strip()
        target_std = term_data.get("target_standard", "").strip()
        category = term_data.get("category", "Key Concept")

        if not original or not target_std:
            continue

        terms_to_insert.append(
            {
                "video_id": video_id,
                "original_term": original,
                "translated_term": f"[{category}] {target_std}",
                "category": category,
                "frequency": 0,
                "is_standardized": False,
            }
        )

    if not terms_to_insert:
        return

    with SessionLocal() as session:
        existing_terms = (
            session.query(Term.original_term).filter(Term.video_id == video_id).all()
        )
        existing_originals = {t.original_term for t in existing_terms}

        new_terms = [
            t for t in terms_to_insert if t["original_term"] not in existing_originals
        ]

        if new_terms:
            session.bulk_insert_mappings(Term.__mapper__, new_terms)
            session.commit()

        if progress_tracker:
            progress_tracker.info(
                "CONTEXT_TERMS",
                f"Saved {len(new_terms)} new terms from context analysis",
                f"Skipped {len(terms_to_insert) - len(new_terms)} duplicates",
            )


def _save_characters_bulk(
    video_id: str, characters: list[dict[str, Any]], progress_tracker: Any = None
) -> None:
    """Save extracted characters as "Character" terms for name locking.

    A short-drama feature: each recurring character is persisted as a Term with
    category "Character" so the translation pipeline treats the character's name
    as a hard constraint (identical in every scene). The persona/gender/
    relationship metadata stays in the stored ``context_analysis`` JSON, which
    the frontend can read to render richer character cards.
    """
    if not characters:
        return

    terms_to_insert = []
    for char in characters:
        name = (char.get("name") or "").strip()
        target = (char.get("target_translation") or "").strip()
        if not name or not target:
            continue
        terms_to_insert.append(
            {
                "video_id": video_id,
                "original_term": name,
                "translated_term": f"[Character] {target}",
                "category": "Character",
                "frequency": 0,
                "is_standardized": False,
            }
        )

    if not terms_to_insert:
        return

    with SessionLocal() as session:
        existing_terms = (
            session.query(Term.original_term).filter(Term.video_id == video_id).all()
        )
        existing_originals = {t.original_term for t in existing_terms}

        new_terms = [
            t for t in terms_to_insert if t["original_term"] not in existing_originals
        ]

        if new_terms:
            session.bulk_insert_mappings(Term.__mapper__, new_terms)
            session.commit()

        if progress_tracker:
            progress_tracker.info(
                "CONTEXT_CHARACTERS",
                f"Saved {len(new_terms)} characters from context analysis",
                f"Skipped {len(terms_to_insert) - len(new_terms)} duplicates",
            )


def get_context_glossary(video_id: str) -> dict[str, str]:
    """
    Extract the context glossary from video's context_analysis.

    Args:
        video_id: ID of the video

    Returns:
        Dictionary mapping English terms to target language translations
    """
    with SessionLocal() as session:
        video = session.query(Video).filter(Video.id == video_id).first()
        if not video or not video.context_analysis:
            return {}

        try:
            context_data = json.loads(video.context_analysis)
            glossary = {}

            for term in context_data.get("key_terms", []):
                original = term.get("original", "").strip()
                target_std = term.get("target_standard", "").strip()
                if original and target_std:
                    glossary[original.lower()] = target_std

            for entity in context_data.get("named_entities", []):
                name = entity.get("name", "").strip()
                target_trans = entity.get("target_translation", "").strip()
                if name and target_trans:
                    glossary[name.lower()] = target_trans

            return glossary
        except (json.JSONDecodeError, KeyError):
            return {}


# Pass 2 ("Glossary Agent") has been removed — its job is now done in the
# same call as analyze_video_context() (see module docstring). It used to
# re-extract a near-duplicate term list from the same full transcript a
# second time; the merge halves the terminology-stage token cost and
# removes the after-the-fact de-duplication this function used to need.


# Backward compatibility alias
analyze_context = analyze_video_context
