#!/usr/bin/env python3
"""下载驾驶员考试网科目一、科目四顺序练习题库。

只依赖 Python 标准库。脚本会动态读取页面中的签名清单地址，下载题目、官方解析
和题目图片，并生成 JSON、CSV、ZIP 及校验摘要。中断后可复用缓存继续运行。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import mimetypes
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


SUBJECTS = {
    "km1": {"name": "科目一", "page": "https://m.jsyks.com/km1_sxlx", "id_prefix": 1},
    "km4": {"name": "科目四", "page": "https://m.jsyks.com/km4_sxlx", "id_prefix": 4},
}
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
PRINT_LOCK = threading.Lock()


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.parts.append("\n")

    def get_text(self) -> str:
        text = "".join(self.parts).replace("\xa0", " ")
        return "\n".join(line.strip() for line in text.splitlines() if line.strip()).strip()


class SafeExplanationHtml(HTMLParser):
    """只保留解析正文中的粗体和换行，不保留属性或其他标签。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "b":
            self.parts.append("<b>")
        elif tag == "br":
            self.parts.append("<br>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            self.parts.append("<br>")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "b":
            self.parts.append("</b>")


def sanitize_explanation_html(value: str) -> str:
    parser = SafeExplanationHtml()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.parts).strip()


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    parser.close()
    return parser.get_text()


def fetch_bytes(url: str, *, referer: str | None = None, retries: int = 4, delay: float = 0.15) -> tuple[bytes, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
                content_type = response.headers.get_content_type()
            if delay:
                time.sleep(delay)
            return body, content_type
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(min(2 ** attempt, 8))
    raise RuntimeError(f"下载失败: {url}: {last_error}")


@dataclass(frozen=True)
class SubjectManifest:
    key: str
    name: str
    page_url: str
    list_url: str
    version: str
    declared_count: int
    codes: list[str]


def extract_assignment(source: str, variable: str, quoted: bool = True) -> str:
    if quoted:
        match = re.search(rf'{re.escape(variable)}\s*=\s*"([^"]*)"', source)
    else:
        match = re.search(rf"{re.escape(variable)}\s*=\s*(\d+)", source)
    if not match:
        raise ValueError(f"页面脚本中没有找到 {variable}")
    return match.group(1)


def load_manifest(subject_key: str) -> SubjectManifest:
    config = SUBJECTS[subject_key]
    page_url = str(config["page"])
    page, _ = fetch_bytes(page_url, delay=0)
    page_text = page.decode("utf-8", "replace")
    match = re.search(r'<script\s+src="(https://tk\.mnks\.cn/lianxiti/[^\"]+)', page_text)
    if not match:
        raise ValueError(f"{page_url} 未发现题目清单接口")
    list_url = html.unescape(match.group(1))
    list_body, _ = fetch_bytes(list_url, referer=page_url, delay=0)
    source = list_body.decode("utf-8", "replace")
    status = extract_assignment(source, "ExamStatus")
    if status != "200":
        message = extract_assignment(source, "ExamMsg")
        raise RuntimeError(f"{subject_key} 清单状态 {status}: {message}")
    version = extract_assignment(source, "ExamVersion")
    declared = int(extract_assignment(source, "ExamCount", quoted=False))
    codes = [code for code in extract_assignment(source, "ExamCodes").split(",") if code]
    if declared != len(codes):
        raise ValueError(f"{subject_key} 声明 {declared} 题，但清单实际 {len(codes)} 题")
    if len(set(codes)) != len(codes):
        raise ValueError(f"{subject_key} 清单中存在重复题目代码")
    return SubjectManifest(
        key=subject_key,
        name=str(config["name"]),
        page_url=page_url,
        list_url=list_url,
        version=version,
        declared_count=declared,
        codes=codes,
    )


def data_url(code: str, version: str) -> str:
    return f"https://tkdata.mnks.cn/ExamData/{code}.json?CALL=?{version}.json"


def note_url(code: str, version: str) -> str:
    return f"https://tkdata.mnks.cn/ExamNote/{code}.json?CALL=?{version}_Note.json"


def image_url(tp: str) -> str:
    if tp.startswith(("http://", "https://")):
        return tp
    stem = tp.rsplit(".", 1)[0] if "." in tp else tp
    return f"https://tkimg.mnks.cn/i/{stem}.webp/jsyks"


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def cached_question(code: str, version: str, cache_dir: Path, referer: str) -> dict[str, Any]:
    path = cache_dir / "data" / f"{code}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    body, _ = fetch_bytes(data_url(code, version), referer=referer)
    value = json.loads(body.decode("utf-8"))
    if value.get("code") and value["code"] != code:
        raise ValueError(f"题目代码不一致: 请求 {code}, 返回 {value['code']}")
    atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return value


def cached_official_note(code: str, version: str, cache_dir: Path, referer: str) -> dict[str, str]:
    path = cache_dir / "notes_html" / f"{code}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        body, _ = fetch_bytes(note_url(code, version), referer=referer)
        notes = json.loads(body.decode("utf-8"))
        official = next((entry for entry in notes if entry.get("type") == 1), None)
        raw_html = str(official.get("cnt", "")) if official else ""
        status = "ok" if official else "missing_official"
    except RuntimeError:
        raw_html = ""
        status = "endpoint_unavailable"
    value = {
        "html": sanitize_explanation_html(raw_html),
        "text": html_to_text(raw_html),
        "status": status,
    }
    atomic_write(path, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return value


def download_one(code: str, version: str, cache_dir: Path, referer: str, include_notes: bool) -> dict[str, Any]:
    raw = cached_question(code, version, cache_dir, referer)
    note = cached_official_note(code, version, cache_dir, referer) if include_notes else {"html": "", "text": ""}
    return {"raw": raw, "note": note}


def split_question(raw: dict[str, Any]) -> tuple[str, list[str]]:
    chunks = re.split(r"<br\s*/?>", str(raw.get("tm", "")), flags=re.IGNORECASE)
    chunks = [html_to_text(chunk) for chunk in chunks]
    chunks = [chunk for chunk in chunks if chunk]
    question = chunks[0] if chunks else ""
    qtype = int(raw.get("tx") or 0)
    if qtype == 1:
        return question, ["正确", "错误"]
    options: list[str] = []
    for chunk in chunks[1:]:
        cleaned = re.sub(r"^[A-HＡ-Ｈ]\s*[、.．:：]\s*", "", chunk, flags=re.IGNORECASE)
        options.append(cleaned.strip())
    return question, options


def normalize_answer(raw_answer: str, qtype: int, option_count: int) -> tuple[list[int], str]:
    value = html_to_text(raw_answer).upper().replace(" ", "")
    if qtype == 1:
        if value in {"对", "正确", "A", "TRUE", "T", "√"}:
            return [0], "A"
        if value in {"错", "错误", "B", "FALSE", "F", "×"}:
            return [1], "B"
    letters = [char for char in value if "A" <= char <= "H"]
    indexes = sorted({ord(char) - 65 for char in letters if ord(char) - 65 < option_count})
    return indexes, "".join(chr(65 + index) for index in indexes)


def normalize_record(subject: SubjectManifest, order: int, code: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload["raw"]
    question, options = split_question(raw)
    tx = int(raw.get("tx") or 0)
    type_name = {1: "judge", 2: "single", 3: "multi"}.get(tx, "unknown")
    answer_indexes, answer_letters = normalize_answer(str(raw.get("da", "")), tx, len(options))
    record: dict[str, Any] = {
        "id": int(SUBJECTS[subject.key]["id_prefix"]) * 10_000_000 + int(code, 16),
        "subject": subject.name,
        "order": order,
        "source_code": code,
        "category_id": raw.get("sortId"),
        "type": type_name,
        "question": question,
        "options": options,
        "answer_letters": answer_letters,
        "explanation": payload["note"]["text"],
        "explanation_html": payload["note"]["html"],
        "explanation_status": payload["note"].get(
            "status", "ok" if payload["note"]["text"] or payload["note"]["html"] else "endpoint_unavailable"
        ),
        "image_url": image_url(str(raw.get("tp", ""))) if raw.get("tp") else "",
        "image_file": "",
        "source_url": data_url(code, subject.version),
    }
    if type_name == "multi":
        record["answers"] = answer_indexes
    else:
        record["answer"] = answer_indexes[0] if answer_indexes else None
    return record


def guess_image_extension(url: str, content_type: str) -> str:
    if content_type == "image/webp":
        return ".webp"
    guessed = mimetypes.guess_extension(content_type) if content_type.startswith("image/") else None
    if guessed:
        return ".jpg" if guessed == ".jpe" else guessed
    suffix = Path(urllib.parse.urlsplit(url).path).suffix.lower()
    return suffix if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"} else ".img"


def download_image(record: dict[str, Any], images_dir: Path, referer: str) -> tuple[str, str]:
    url = record["image_url"]
    if not url:
        return record["source_code"], ""
    existing = list(images_dir.glob(f"{record['source_code']}.*"))
    if existing:
        return record["source_code"], existing[0].name
    body, content_type = fetch_bytes(url, referer=referer, delay=0.1)
    extension = guess_image_extension(url, content_type)
    name = f"{record['source_code']}{extension}"
    atomic_write(images_dir / name, body)
    return record["source_code"], name


def progress(label: str, done: int, total: int) -> None:
    if done == total or done % 100 == 0:
        with PRINT_LOCK:
            print(f"[{label}] {done}/{total}", flush=True)


def parallel_map(items: list[Any], worker, *, workers: int, label: str) -> dict[Any, Any]:
    results: dict[Any, Any] = {}
    failures: list[tuple[Any, str]] = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        for done, future in enumerate(as_completed(futures), 1):
            item = futures[future]
            try:
                results[item] = future.result()
            except Exception as exc:  # noqa: BLE001 - aggregate all failures for a useful report
                failures.append((item, str(exc)))
            progress(label, done, len(items))
    if failures:
        preview = "\n".join(f"  {item}: {error}" for item, error in failures[:20])
        raise RuntimeError(f"{label} 失败 {len(failures)} 项:\n{preview}")
    return results


def validate_records(
    manifest: SubjectManifest,
    records: list[dict[str, Any]],
    include_notes: bool,
    include_images: bool,
    images_dir: Path,
) -> dict[str, Any]:
    errors: list[str] = []
    seen_codes: set[str] = set()
    type_counts: dict[str, int] = {}
    image_count = 0
    missing_image_files: list[str] = []
    missing_notes = 0
    explanation_status_counts: dict[str, int] = {}
    html_tag_counts: dict[str, int] = {}
    invalid_html_tags: set[str] = set()
    plain_text_tag_residues: list[str] = []
    for record in records:
        code = record["source_code"]
        if code in seen_codes:
            errors.append(f"重复代码 {code}")
        seen_codes.add(code)
        if not record["question"]:
            errors.append(f"{code} 题干为空")
        if record["type"] == "unknown":
            errors.append(f"{code} 题型未知")
        answer_indexes = record.get("answers", [record.get("answer")])
        if not answer_indexes or any(index is None or index < 0 or index >= len(record["options"]) for index in answer_indexes):
            errors.append(f"{code} 答案越界或为空")
        type_counts[record["type"]] = type_counts.get(record["type"], 0) + 1
        image_count += bool(record["image_url"])
        missing_notes += include_notes and not bool(record["explanation"])
        if include_images and record["image_url"] and (
            not record["image_file"] or not (images_dir / record["image_file"]).is_file()
        ):
            missing_image_files.append(code)
        status = record["explanation_status"]
        explanation_status_counts[status] = explanation_status_counts.get(status, 0) + 1
        for match in re.finditer(r"<\s*(/?)\s*([A-Za-z0-9]+)(?:\s[^>]*)?>", record["explanation_html"]):
            tag = match.group(2).lower()
            html_tag_counts[tag] = html_tag_counts.get(tag, 0) + 1
            if tag not in {"b", "br"}:
                invalid_html_tags.add(tag)
        if re.search(r"<\s*/?\s*[A-Za-z][^>]*>", record["explanation"]):
            plain_text_tag_residues.append(code)
    if len(records) != manifest.declared_count:
        errors.append(f"记录数 {len(records)} 与清单 {manifest.declared_count} 不一致")
    if missing_image_files:
        errors.append(f"缺少 {len(missing_image_files)} 个图片文件")
    if invalid_html_tags:
        errors.append(f"解析 HTML 含非白名单标签: {sorted(invalid_html_tags)}")
    if plain_text_tag_residues:
        errors.append(f"纯文本解析仍含 HTML 标签: {len(plain_text_tag_residues)} 题")
    return {
        "subject": manifest.name,
        "version": manifest.version,
        "declared_count": manifest.declared_count,
        "record_count": len(records),
        "unique_codes": len(seen_codes),
        "type_counts": type_counts,
        "image_questions": image_count,
        "missing_image_files": len(missing_image_files),
        "missing_official_explanations": int(missing_notes),
        "explanation_status_counts": explanation_status_counts,
        "html_tag_counts": html_tag_counts,
        "invalid_html_tags": sorted(invalid_html_tags),
        "plain_text_tag_residues": len(plain_text_tag_residues),
        "errors": errors,
        "valid": not errors,
    }


def write_json(path: Path, manifest: SubjectManifest, records: list[dict[str, Any]], captured_at: str) -> None:
    payload = {
        "source": manifest.page_url,
        "source_list": manifest.list_url,
        "version": manifest.version,
        "captured_at": captured_at,
        "count": len(records),
        "questions": records,
    }
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))


def write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["题号", "科目", "分类", "题型", "题干", "选项A", "选项B", "选项C", "选项D", "答案", "解析", "解析HTML", "解析状态", "图片", "来源代码", "来源地址"])
        type_labels = {"judge": "判断题", "single": "单选题", "multi": "多选题"}
        for record in records:
            options = list(record["options"]) + [""] * 4
            writer.writerow([
                record["order"],
                record["subject"],
                record["category_id"],
                type_labels.get(record["type"], record["type"]),
                record["question"],
                *options[:4],
                record["answer_letters"],
                record["explanation"],
                record["explanation_html"],
                record["explanation_status"],
                record["image_file"],
                record["source_code"],
                record["source_url"],
            ])


def write_zip(path: Path, csv_path: Path, records: list[dict[str, Any]], images_dir: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        archive.write(csv_path, "questions.csv")
        image_names = sorted({record["image_file"] for record in records if record["image_file"]})
        for name in image_names:
            archive.write(images_dir / name, f"images/{name}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="下载科目一和科目四完整题库")
    parser.add_argument("--subjects", nargs="+", choices=sorted(SUBJECTS), default=["km1", "km4"])
    parser.add_argument("--output", type=Path, default=Path("data/jsyks"))
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--without-notes", action="store_true", help="不下载官方解析")
    parser.add_argument("--without-images", action="store_true", help="不下载题目图片")
    args = parser.parse_args()
    if not 1 <= args.workers <= 8:
        parser.error("--workers 必须在 1 到 8 之间")

    output = args.output.resolve()
    cache_dir = output / ".cache"
    images_dir = output / "images"
    output.mkdir(parents=True, exist_ok=True)
    manifests = [load_manifest(key) for key in args.subjects]
    for manifest in manifests:
        print(f"[清单] {manifest.name}: {manifest.declared_count} 题，版本 {manifest.version}")

    code_versions: dict[str, tuple[str, str]] = {}
    for manifest in manifests:
        for code in manifest.codes:
            previous = code_versions.get(code)
            if previous and previous[0] != manifest.version:
                raise ValueError(f"同一代码 {code} 出现不同版本")
            code_versions[code] = (manifest.version, manifest.page_url)
    unique_codes = sorted(code_versions)
    print(f"[下载] 清单合计 {sum(m.declared_count for m in manifests)} 题，去重后 {len(unique_codes)} 个题目代码")
    payloads = parallel_map(
        unique_codes,
        lambda code: download_one(code, code_versions[code][0], cache_dir, code_versions[code][1], not args.without_notes),
        workers=args.workers,
        label="题目",
    )

    records_by_subject: dict[str, list[dict[str, Any]]] = {}
    for manifest in manifests:
        records_by_subject[manifest.key] = [
            normalize_record(manifest, order, code, payloads[code])
            for order, code in enumerate(manifest.codes, 1)
        ]

    if not args.without_images:
        image_records: dict[str, dict[str, Any]] = {}
        for records in records_by_subject.values():
            for record in records:
                if record["image_url"]:
                    image_records.setdefault(record["source_code"], record)
        images_dir.mkdir(parents=True, exist_ok=True)
        image_files = parallel_map(
            sorted(image_records),
            lambda code: download_image(image_records[code], images_dir, manifests[0].page_url)[1],
            workers=args.workers,
            label="图片",
        )
        for records in records_by_subject.values():
            for record in records:
                record["image_file"] = image_files.get(record["source_code"], "")

    captured_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    validations: dict[str, Any] = {}
    deliverables: list[Path] = []
    for manifest in manifests:
        records = records_by_subject[manifest.key]
        validation = validate_records(
            manifest,
            records,
            not args.without_notes,
            not args.without_images,
            images_dir,
        )
        validations[manifest.key] = validation
        if not validation["valid"]:
            raise ValueError(f"{manifest.name} 校验失败: {validation['errors'][:20]}")
        json_path = output / f"{manifest.key}_questions.json"
        csv_path = output / f"{manifest.key}_questions.csv"
        zip_path = output / f"{manifest.key}_questions.zip"
        write_json(json_path, manifest, records, captured_at)
        write_csv(csv_path, records)
        write_zip(zip_path, csv_path, records, images_dir)
        deliverables.extend([json_path, csv_path, zip_path])
        print(f"[完成] {manifest.name}: {len(records)} 题")

    summary = {
        "captured_at": captured_at,
        "source": "https://m.jsyks.com/",
        "subjects": validations,
        "files": [
            {"path": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)}
            for path in deliverables
        ],
    }
    summary_path = output / "validation_summary.json"
    atomic_write(summary_path, json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"))
    print(f"[校验] {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
