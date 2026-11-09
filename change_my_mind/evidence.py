import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
import trafilatura
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .errors import EvidenceError
from .schemas import ClaimDraft, RecordView
from .store import Store, now


@dataclass
class Page:
    url: str
    title: str
    raw: bytes
    sections: list[tuple[str, str]]
    media_type: str = "text/html"
    synthetic: bool = False


class SourceFetcher(Protocol):
    async def fetch(self, url: str) -> Page: ...


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", query, ""))


async def validate_public_url(url: str):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise EvidenceError("Only public HTTP(S) source URLs are supported")
    if parts.port and parts.port not in {80, 443}:
        raise EvidenceError("Source URLs must use standard web ports")
    loop = asyncio.get_running_loop()
    addresses = await loop.getaddrinfo(parts.hostname, parts.port or 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise EvidenceError("Private, local, and reserved network addresses are not research sources")


class HTTPSourceFetcher:
    async def fetch(self, url: str) -> Page:
        async with httpx.AsyncClient(
            timeout=25,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "ChangeMyMind/0.1 public-source research"},
        ) as client:
            for _ in range(6):
                await validate_public_url(url)
                async with client.stream("GET", url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 20_000_000:
                            raise EvidenceError("Source exceeds the 20 MB retrieval limit")
                    raw = bytes(body)
                    media = response.headers.get("content-type", "").split(";")[0]
                    if raw.startswith(b"%PDF"):

                        def extract_pdf():
                            reader = PdfReader(BytesIO(raw))
                            sections = [
                                (f"page {i + 1}", page.extract_text() or "")
                                for i, page in enumerate(reader.pages)
                            ]
                            title = str(
                                (reader.metadata or {}).get("/Title") or urlsplit(url).path.rsplit("/", 1)[-1]
                            )
                            return Page(url, title, raw, sections, "application/pdf")

                        return await asyncio.to_thread(extract_pdf)
                    if media and not any(t in media for t in ["html", "text", "xml"]):
                        raise EvidenceError(f"Unsupported source media type: {media}")
                    text = await asyncio.to_thread(
                        trafilatura.extract, raw, include_tables=True, include_comments=False
                    )
                    if not text:
                        raise EvidenceError("No readable source text; it may require sign-in or JavaScript")
                    metadata = await asyncio.to_thread(trafilatura.extract_metadata, raw)
                    return Page(
                        url,
                        (metadata.title if metadata else None) or urlsplit(url).hostname or url,
                        raw,
                        [("article", text)],
                        media or "text/html",
                    )
        raise EvidenceError("Too many redirects")


class Evidence:
    def __init__(self, store: Store, fetcher: SourceFetcher):
        self.store, self.fetcher = store, fetcher
        self.semaphore = asyncio.Semaphore(4)

    async def retrieve(self, run_id, url, *, team=None, stage="research", query="") -> RecordView:
        url = canonical_url(url)
        key = f"{team or 'shared'}:{hashlib.sha256(url.encode()).hexdigest()[:20]}"
        existing = await self.store.by_key(run_id, "source", key)
        if existing:
            return existing
        try:
            async with self.semaphore:
                page = await self.fetcher.fetch(url)
            text = "\n\n".join(text for _, text in page.sections)
            if not text.strip():
                raise EvidenceError("Document has no extractable text; scanned PDFs require OCR")
            directory = self.store.data_dir / "evidence" / run_id
            directory.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(page.raw).hexdigest()
            extension = "pdf" if page.media_type == "application/pdf" else "html"
            raw_path, text_path = directory / f"{digest}.{extension}", directory / f"{digest}.txt"
            sections_path = directory / f"{digest}.sections.json"
            await asyncio.to_thread(raw_path.write_bytes, page.raw)
            await asyncio.to_thread(text_path.write_text, text)
            await asyncio.to_thread(sections_path.write_text, json.dumps(page.sections))
            dois = sorted(set(re.findall(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", text)))
            record = await self.store.put(
                run_id,
                "source",
                key,
                {
                    "url": page.url,
                    "requested_url": url,
                    "title": page.title,
                    "retrieved_at": now(),
                    "access_status": "retrieved",
                    "raw_path": str(raw_path.resolve()),
                    "text_path": str(text_path.resolve()),
                    "content_hash": digest,
                    "media_type": page.media_type,
                    "underlying_evidence_ids": dois,
                    "sections_path": str(sections_path.resolve()),
                    "synthetic": page.synthetic,
                    "query": query,
                },
                team=team,
                stage=stage,
                public=team is None,
            )
            chunks = self.chunks(page.sections)
            words = set(re.findall(r"\w{3,}", query.lower()))

            def score(chunk):
                return len(words & set(re.findall(r"\w{3,}", chunk[1].lower())))

            selected = sorted(chunks, key=score, reverse=True)[:32]
            for key_suffix, quote, location in selected:
                await self.register_passage(
                    run_id, record, quote, location, f"{record.id}:{key_suffix}", team, stage
                )
            return record
        except (httpx.HTTPError, EvidenceError, OSError, ValueError, PyPdfError) as error:
            return await self.store.put(
                run_id,
                "source",
                key,
                {
                    "url": url,
                    "title": urlsplit(url).hostname,
                    "retrieved_at": now(),
                    "access_status": "unavailable",
                    "limitation": str(error),
                    "query": query,
                },
                team=team,
                stage=stage,
                public=team is None,
            )

    @staticmethod
    def chunks(sections):
        chunks = []
        for section_index, (location, section) in enumerate(sections):
            for paragraph_index, paragraph in enumerate(re.split(r"\n\s*\n|\n", section)):
                paragraph = paragraph.strip()
                if len(paragraph) < 25:
                    continue
                # Full text is retained on disk; index relevant bounded passages on demand.
                for start in range(0, len(paragraph), 2500):
                    quote = paragraph[start : start + 2500]
                    chunks.append(
                        (
                            f"{section_index}:{paragraph_index}:{start}",
                            quote,
                            f"{location}, paragraph {paragraph_index + 1}, chars {start}–{start + len(quote)}",
                        )
                    )
        return chunks

    async def register_passage(self, run_id, source, quote, location, key, team=None, stage="research"):
        if source.data.get("access_status") != "retrieved":
            raise EvidenceError("Search snippets and inaccessible sources cannot supply verified passages")
        text = await asyncio.to_thread(Path(source.data["text_path"]).read_text)
        if not quote.strip() or quote not in text:
            raise EvidenceError("Quotation does not appear verbatim in the retrieved source")
        passage = await self.store.put(
            run_id,
            "passage",
            key,
            {
                "source_id": source.id,
                "quote": quote,
                "location": location,
                "retrieved_at": source.data["retrieved_at"],
                "synthetic": source.data.get("synthetic", False),
            },
            team=team,
            stage=stage,
            public=team is None,
        )
        await self.store.link(run_id, source.id, passage.id)
        return passage

    async def relevant_passages(self, run_id, query, audience, *, source_id=None, limit=20):
        words = set(re.findall(r"\w{3,}", query.lower()))
        passages = await self.store.records(run_id, audience=audience, kinds=["passage"])
        if source_id:
            source = await self.store.get_record(run_id, source_id, audience=audience)
            if source.kind != "source":
                raise EvidenceError("Additional retrieval requires a registered source")
            if source.data.get("sections_path"):
                sections = json.loads(await asyncio.to_thread(Path(source.data["sections_path"]).read_text))
                candidates = self.chunks(sections)
                candidates.sort(
                    key=lambda c: len(words & set(re.findall(r"\w{3,}", c[1].lower()))), reverse=True
                )
                run = await self.store.get_run(run_id, records=False)
                for key_suffix, quote, location in candidates[:limit]:
                    await self.register_passage(
                        run_id,
                        source,
                        quote,
                        location,
                        f"{source.id}:{key_suffix}",
                        audience if audience in {"A", "B"} and source.visibility != "public" else None,
                        run.stage,
                    )
                passages = await self.store.records(run_id, audience=audience, kinds=["passage"])
            passages = [p for p in passages if p.data["source_id"] == source_id]

        def score(p):
            return len(words & set(re.findall(r"\w{3,}", p.data["quote"].lower())))

        return sorted(passages, key=score, reverse=True)[:limit]

    async def register_claim(self, run_id, key, claim: ClaimDraft, team, stage, *, public=False):
        for passage_id in claim.supporting_passage_ids + claim.conflicting_passage_ids:
            passage = await self.store.get_record(run_id, passage_id, audience=team or "observer")
            if passage.kind != "passage":
                raise EvidenceError("Claims must cite retrieved passage IDs")
        data = {**claim.model_dump(), "status": "unchecked", "origin": team or stage}
        if claim.revises_claim_id:
            result = await self.store.revise_claim(run_id, claim.revises_claim_id, key, data, team, stage)
        else:
            result = await self.store.put(run_id, "claim", key, data, team=team, stage=stage, public=public)
        for passage_id in claim.supporting_passage_ids + claim.conflicting_passage_ids:
            await self.store.link(run_id, passage_id, result.id)
        return result
