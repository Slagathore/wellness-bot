"""
Resource Ingestion Module

Mission Statement:
This module handles ingestion of wellness resources from various formats
(markdown, text, PDF, web pages) into the vector store. It processes
and structures content for optimal retrieval.

Features:
- Markdown file parsing
- Document metadata extraction
- Automatic categorization
- Batch processing

#todo: Add PDF support using PyPDF2
#todo: Add web scraping for trusted sources (CDC, NIMH, etc.)
#todo: Implement automatic refresh/update checking
#todo: Add document quality scoring
"""

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from .vector_store import WellnessVectorStore
from app.utils.time_utils import operator_now

logger = logging.getLogger(__name__)


class ResourceIngester:
    """Ingest wellness resources into vector store"""

    # Wellness categories
    CATEGORIES = {
        "anxiety": ["anxiety", "worry", "panic", "stress", "nervous"],
        "depression": ["depression", "sad", "hopeless", "suicide", "self-harm"],
        "sleep": ["sleep", "insomnia", "rest", "tired", "fatigue"],
        "adhd": ["adhd", "attention", "focus", "concentration", "executive"],
        "trauma": ["trauma", "ptsd", "abuse", "assault"],
        "relationships": ["relationship", "family", "friends", "social", "connection"],
        "self-care": ["self-care", "wellness", "health", "exercise", "nutrition"],
        "therapy": ["therapy", "cbt", "dbt", "counseling", "treatment"],
        "crisis": ["crisis", "emergency", "hotline", "help", "support"],
    }

    def __init__(self, vector_store: WellnessVectorStore):
        """
        Initialize resource ingester

        Args:
            vector_store: WellnessVectorStore instance
        """
        self.vector_store = vector_store
        state_dir = Path(self.vector_store.db_path).parent
        self.state_path = state_dir / "ingest_state.json"
        self._refresh_state = self._load_state()

    def _load_state(self) -> Dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning(
                    "[RAG] Unable to parse ingest state file; starting fresh"
                )
        return {}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self._refresh_state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[RAG] Failed to persist ingest state: {exc}")

    def ingest_directory(
        self,
        directory: str,
        recursive: bool = True,
        category_override: str | None = None,
    ) -> int:
        """
        Ingest all wellness documents from a directory

        Args:
            directory: Path to directory containing wellness resources
            recursive: Whether to search subdirectories

        Returns:
            Number of documents ingested
        """
        path = Path(directory)

        if not path.exists():
            logger.error(f"Directory not found: {directory}")
            return 0

        # Find all markdown files
        if recursive:
            files = list(path.rglob("*.md")) + list(path.rglob("*.txt"))
        else:
            files = list(path.glob("*.md")) + list(path.glob("*.txt"))

        logger.info(f"[RAG] Found {len(files)} files in {directory}")

        # Process files
        documents = []
        for file_path in files:
            try:
                doc = self._parse_file(file_path, category_override=category_override)
                if doc:
                    documents.append(doc)
            except Exception as e:
                logger.error(f"Error parsing {file_path}: {e}")

        # Add to vector store
        if documents:
            added = self.vector_store.add_documents(documents)
            logger.info(f"[RAG] Ingested {added} documents from {directory}")
            return added

        return 0

    def refresh_from_manifest(
        self, manifest_path: str | Path, force: bool = False
    ) -> int:
        """Refresh wellness resources based on a JSON manifest."""

        manifest_path = Path(manifest_path)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        if not manifest_path.exists():
            self._write_sample_manifest(manifest_path)

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.error(f"[RAG] Manifest parsing failed: {exc}")
            return 0

        total_added = 0
        now = operator_now()

        for entry in manifest:
            entry_id = entry.get("id") or entry.get("path") or entry.get("url")
            if not entry_id:
                logger.warning("[RAG] Manifest entry missing id/path/url: %s", entry)
                continue

            frequency_days = entry.get("frequency_days", 7)
            state_info = self._refresh_state.get(entry_id, {})
            last_run_iso = state_info.get("last_run")
            last_run = None
            if last_run_iso:
                try:
                    last_run = datetime.fromisoformat(last_run_iso)
                except ValueError:
                    last_run = None

            if (
                not force
                and last_run
                and now - last_run < timedelta(days=frequency_days)
            ):
                continue

            latest_mtime = None
            added = 0
            source_type = entry.get("type", "file")

            try:
                if source_type == "directory":
                    directory = entry.get("path")
                    if not directory:
                        continue
                    path_obj = Path(directory)
                    if not path_obj.exists():
                        logger.warning(
                            "[RAG] Directory not found in manifest: %s", directory
                        )
                        continue
                    latest_mtime = self._get_directory_mtime(directory)
                    if (
                        not force
                        and latest_mtime
                        and state_info.get("last_mtime") == latest_mtime
                    ):
                        continue
                    added = self.ingest_directory(
                        directory,
                        recursive=entry.get("recursive", True),
                        category_override=entry.get("category"),
                    )
                elif source_type == "file":
                    file_path = entry.get("path")
                    if not file_path:
                        continue
                    doc = self._parse_file(
                        Path(file_path), category_override=entry.get("category")
                    )
                    if doc:
                        added = self.vector_store.add_documents([doc])
                elif source_type == "url":
                    url = entry.get("url")
                    if not url:
                        continue
                    doc = self._ingest_url(url, category_hint=entry.get("category"))
                    if doc:
                        added = self.vector_store.add_documents([doc])
                else:
                    logger.warning(
                        "[RAG] Unknown manifest source type: %s", source_type
                    )
                    continue
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"[RAG] Failed processing manifest entry {entry_id}: {exc}",
                    exc_info=True,
                )
                continue

            if added:
                total_added += added
            self._refresh_state[entry_id] = {
                "last_run": now.isoformat(),
                "last_mtime": latest_mtime,
                "last_added": added,
            }

        self._save_state()
        return total_added

    def _write_sample_manifest(self, manifest_path: Path) -> None:
        sample = [
            {
                "id": "medline_stress",
                "type": "url",
                "url": "https://medlineplus.gov/ency/article/001942.htm",
                "category": "stress",
                "frequency_days": 14,
            },
            {
                "id": "nih_sleep",
                "type": "url",
                "url": "https://www.nhlbi.nih.gov/health/sleep-deprivation",
                "category": "sleep",
                "frequency_days": 21,
            },
            {
                "id": "local_handbook",
                "type": "directory",
                "path": "wellness_data/resources/manual",
                "recursive": True,
                "frequency_days": 7,
            },
        ]
        manifest_path.write_text(json.dumps(sample, indent=2), encoding="utf-8")
        logger.info(f"[RAG] Created sample manifest at {manifest_path}")

    def _get_directory_mtime(self, directory: str) -> Optional[float]:
        path = Path(directory)
        if not path.exists():
            return None
        mtimes = [item.stat().st_mtime for item in path.rglob("*") if item.is_file()]
        return max(mtimes) if mtimes else None

    def _parse_file(
        self, file_path: Path, category_override: str | None = None
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a single file into a document dict

        Args:
            file_path: Path to file

        Returns:
            Document dict or None if parsing failed
        """
        try:
            # Read file
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Extract title (first heading or filename)
            title = self._extract_title(content, file_path.stem)

            # Auto-detect category (allow override from manifest)
            category = category_override or self._detect_category(
                content, file_path.stem
            )

            # Generate doc ID from filename
            doc_id = f"wellness_{file_path.stem.lower().replace(' ', '_')}"

            # Extract metadata from frontmatter if present
            metadata = self._extract_metadata(content)

            # Remove frontmatter from content
            content = self._clean_content(content)

            if not content.strip():
                logger.warning(f"Empty content in {file_path}")
                return None

            return {
                "doc_id": doc_id,
                "title": title,
                "content": content,
                "category": category,
                "source": metadata.get("source", "local"),
                "url": metadata.get("url"),
                "metadata": {
                    "filename": file_path.name,
                    "file_path": str(file_path),
                    **metadata,
                },
            }

        except Exception as e:
            logger.error(f"Error parsing file {file_path}: {e}")
            return None

    def _ingest_url(
        self, url: str, category_hint: str | None = None
    ) -> Optional[Dict[str, Any]]:
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[RAG] Failed to fetch URL {url}: {exc}")
            return None

        content = self._clean_html(response.text)
        if not content:
            return None

        parsed = urlparse(url)
        fallback_name = parsed.path.split("/")[-1] or parsed.netloc
        title = self._extract_title(content, fallback_name)
        category = category_hint or self._detect_category(content, fallback_name)
        doc_id = f"url_{hashlib.md5(url.encode('utf-8')).hexdigest()}"

        return {
            "doc_id": doc_id,
            "title": title,
            "content": content,
            "category": category,
            "source": "url",
            "url": url,
            "metadata": {
                "fetched_at": operator_now().isoformat(),
                "url": url,
            },
        }

    def _clean_html(self, html: str) -> str:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
        text = re.sub(r"(?i)<br\s*/?>", "\n", text)
        text = re.sub(r"(?i)</(p|li|div|h[1-6])>", "\n", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _extract_title(self, content: str, fallback: str) -> str:
        """Extract title from content or use fallback"""
        # Look for first markdown heading
        match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        # Use filename as fallback
        return fallback.replace("_", " ").replace("-", " ").title()

    def _detect_category(self, content: str, filename: str) -> Optional[str]:
        """Auto-detect document category based on content"""
        text = (content + " " + filename).lower()

        # Count keyword matches for each category
        scores: Dict[str, int] = {}
        for category, keywords in self.CATEGORIES.items():
            score = sum(1 for keyword in keywords if keyword in text)
            if score > 0:
                scores[category] = score

        # Return category with highest score
        if scores:
            return max(scores, key=lambda key: scores[key])

        return None

    def _extract_metadata(self, content: str) -> Dict[str, Any]:
        """Extract YAML frontmatter metadata if present"""
        metadata = {}

        # Check for YAML frontmatter (--- at start)
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1].strip()

                # Parse simple key: value pairs
                for line in frontmatter.split("\n"):
                    if ":" in line:
                        key, value = line.split(":", 1)
                        metadata[key.strip()] = value.strip()

        return metadata

    def _clean_content(self, content: str) -> str:
        """Remove frontmatter and clean content"""
        # Remove frontmatter
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                content = parts[2]

        # Remove multiple newlines
        content = re.sub(r"\n{3,}", "\n\n", content)

        return content.strip()

    def ingest_seed_data(self) -> int:
        """
        Ingest default wellness resources

        Returns:
            Number of documents added
        """
        seed_documents = [
            {
                "doc_id": "anxiety_grounding_54321",
                "title": "5-4-3-2-1 Grounding Technique for Anxiety",
                "content": """The 5-4-3-2-1 grounding technique is a powerful tool to manage anxiety and panic attacks.

**How it works:**

1. **5 things you can SEE:** Look around and name 5 things you can see right now.
2. **4 things you can TOUCH:** Notice 4 things you can feel (your clothes, the chair, your feet on the floor).
3. **3 things you can HEAR:** Listen for 3 sounds around you.
4. **2 things you can SMELL:** Identify 2 scents (or think of 2 favorite smells).
5. **1 thing you can TASTE:** Notice 1 taste in your mouth, or think of a favorite flavor.

**Why it works:**
This technique activates your senses and brings you back to the present moment, interrupting anxious thought spirals. It's especially helpful during panic attacks or moments of high stress.

**Practice tip:** You can use this anytime, anywhere. No special equipment needed!""",
                "category": "anxiety",
                "source": "Evidence-based CBT technique",
                "metadata": {"difficulty": "beginner", "time": "2-3 minutes"},
            },
            {
                "doc_id": "sleep_hygiene_basics",
                "title": "Sleep Hygiene: Essential Practices for Better Rest",
                "content": """Good sleep hygiene can dramatically improve sleep quality and mental health.

**Core Principles:**

**Environment:**
- Keep bedroom cool (60-67°F / 15-19°C)
- Make room as dark as possible
- Minimize noise or use white noise
- Reserve bed for sleep only (not work/screens)

**Timing:**
- Consistent sleep/wake schedule (even weekends)
- Aim for 7-9 hours
- Avoid naps after 3 PM

**Before Bed:**
- No screens 1 hour before sleep (blue light disrupts melatonin)
- Avoid caffeine after 2 PM
- Light snack if hungry (avoid heavy meals)
- Relaxing routine: reading, stretching, meditation

**If You Can't Sleep:**
- Don't lie awake for more than 20 minutes
- Get up and do a calm activity
- Return to bed when sleepy

**Why it matters:**
Poor sleep worsens anxiety, depression, and cognitive function. Prioritizing sleep is one of the most impactful wellness practices.""",
                "category": "sleep",
                "source": "Sleep Foundation / CDC guidelines",
                "metadata": {"difficulty": "beginner", "time": "ongoing practice"},
            },
            {
                "doc_id": "crisis_resources_hotlines",
                "title": "Crisis Resources and Hotlines",
                "content": """If you're in crisis, immediate help is available 24/7.

**EMERGENCY: Call 911 or go to nearest ER**

**National Suicide Prevention Lifeline:**
- Phone: 988 (call or text)
- Available 24/7, free, confidential
- Online chat: suicidepreventionlifeline.org

**Crisis Text Line:**
- Text HOME to 741741
- Available 24/7
- Trained crisis counselors

**SAMHSA National Helpline:**
- 1-800-662-4357
- Mental health and substance abuse referrals
- Available 24/7, free

**Trans Lifeline:**
- 1-877-565-8860
- Trans peer support

**Trevor Project (LGBTQ+ Youth):**
- 1-866-488-7386
- Text START to 678678

**RAINN (Sexual Assault):**
- 1-800-656-4673
- Online chat: rainn.org

**Remember:** Asking for help is a sign of strength, not weakness. You don't have to face this alone.""",
                "category": "crisis",
                "source": "National crisis resource compilation",
                "metadata": {"priority": "urgent", "availability": "24/7"},
            },
            {
                "doc_id": "adhd_productivity_tips",
                "title": "ADHD-Friendly Productivity Strategies",
                "content": """Productivity with ADHD requires working WITH your brain, not against it.

**Time Management:**
- Use timers (Pomodoro: 25 min work, 5 min break)
- Time-boxing: allocate specific time blocks
- Build in buffer time (tasks take longer than you think)

**Task Management:**
- Break tasks into TINY steps (15-min chunks)
- Start with easiest task to build momentum
- Use "body doubling" (work alongside someone)
- External accountability (share goals with friend)

**Environment:**
- Minimize distractions (noise-canceling headphones)
- Keep workspace clear (visual clutter = mental clutter)
- Use visual reminders (sticky notes, whiteboards)

**Energy Management:**
- Do hardest tasks during peak energy time
- Movement breaks every 30-60 minutes
- Accept that some days are low-productivity (be kind to yourself)

**Tools:**
- Task apps with reminders (Todoist, Things)
- Calendar blocking (visual schedule)
- Capture ideas immediately (voice notes, quick notes)

**The Golden Rule:**
Don't rely on memory. Externalize everything (write it down, set alarms, visual cues).

**Most Important:**
Self-compassion. ADHD brains work differently, not worse. Find systems that work for YOU.""",
                "category": "adhd",
                "source": "ADHD coaching best practices",
                "metadata": {"difficulty": "intermediate", "audience": "ADHD"},
            },
            {
                "doc_id": "depression_behavioral_activation",
                "title": "Behavioral Activation for Depression",
                "content": """When depressed, we often wait to feel better before doing things. Behavioral Activation reverses this: do things first, feelings follow.

**The Core Concept:**
Depression tells you to withdraw, rest, avoid. But inactivity makes depression WORSE. By engaging in meaningful activities, you break the cycle.

**How to Start:**

**1. Activity Monitoring (Week 1):**
- Track what you do each day and your mood (1-10)
- Notice patterns: what activities improve mood?

**2. Activity Scheduling (Week 2+):**
- Schedule 1-3 meaningful activities per day
- Start SMALL (5-10 minute activities)
- Mix pleasure (things you enjoy) and mastery (accomplishments)

**Example Activities:**

**Pleasure:**
- Listen to favorite song
- Watch funny video
- Pet an animal
- Sit outside for 5 minutes
- Call a friend

**Mastery:**
- Make bed
- Do one dish
- Walk around block
- Organize one drawer
- Send one email

**Key Principles:**
- Start BEFORE you feel motivated (motivation follows action)
- Do it even if you don't enjoy it at first
- Celebrate small wins
- Be consistent (daily practice)

**Why it Works:**
Activity provides:
- Sense of accomplishment
- Distraction from rumination
- Potential for positive experiences
- Structure and routine

**Evidence:** Behavioral Activation is as effective as medication for mild-moderate depression.""",
                "category": "depression",
                "source": "Evidence-based CBT technique",
                "metadata": {
                    "difficulty": "intermediate",
                    "time_commitment": "daily practice",
                },
            },
        ]

        added = self.vector_store.add_documents(seed_documents)
        logger.info(f"[RAG] Added {added} seed wellness documents")
        return added


# Module-level documentation:
# - ResourceIngester: Main class for ingesting wellness documents
# - ingest_directory(): Batch process files from directory
# - ingest_seed_data(): Load default wellness resources
# - _detect_category(): Auto-categorize based on content
# - CATEGORIES: Predefined wellness topic categories

# #todo: Add support for PDF documents (PyPDF2)
# #todo: Web scraper for trusted sources (CDC, NIMH, Mayo Clinic)
# #todo: Automatic duplicate detection and merging
# #todo: Document version tracking (update existing docs)
