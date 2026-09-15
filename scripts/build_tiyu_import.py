#!/usr/bin/env python3
"""把抓取后的题库 JSON 打包为题屿 App 可直接导入的 ZIP。"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any


HEADERS = [
    "题号",
    "题型",
    "题干",
    "图片",
    "选项A",
    "选项B",
    "选项C",
    "选项D",
    "选项E",
    "选项F",
    "选项G",
    "选项H",
    "答案",
    "解析",
    "备注",
]
TYPE_LABELS = {"single": "单选", "multi": "多选", "judge": "判断"}
ALLOWED_TYPES = {"单选", "多选", "判断", "填空", "简答"}
ALLOWED_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
HTML_TAG = re.compile(r"<\s*/?\s*[A-Za-z][^>]*>")


def plain_text(value: Any) -> str:
    """防御性移除标签；输入数据当前已经是纯文本。"""
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ").strip()


def answer_for(record: dict[str, Any], type_label: str) -> str:
    if type_label == "判断":
        answer = record.get("answer")
        if answer == 0:
            return "对"
        if answer == 1:
            return "错"
        raise ValueError(f"题号 {record.get('order')} 的判断题答案无效: {answer!r}")
    letters = str(record.get("answer_letters", "")).upper().strip()
    if type_label == "单选" and not re.fullmatch(r"[A-H]", letters):
        raise ValueError(f"题号 {record.get('order')} 的单选答案无效: {letters!r}")
    if type_label == "多选" and not re.fullmatch(r"[A-H]{2,8}", letters):
        raise ValueError(f"题号 {record.get('order')} 的多选答案无效: {letters!r}")
    if len(set(letters)) != len(letters):
        raise ValueError(f"题号 {record.get('order')} 的答案含重复选项: {letters!r}")
    return letters


def record_to_row(number: int, record: dict[str, Any], images_dir: Path) -> list[Any]:
    type_label = TYPE_LABELS.get(str(record.get("type")))
    if not type_label:
        raise ValueError(f"题号 {number} 的题型不受支持: {record.get('type')!r}")
    options = [plain_text(value) for value in record.get("options", [])]
    if type_label == "判断":
        options = ["对", "错"]
    if type_label in {"单选", "多选"} and not 2 <= len(options) <= 8:
        raise ValueError(f"题号 {number} 的选项数无效: {len(options)}")
    options.extend([""] * (8 - len(options)))

    image_name = str(record.get("image_file", "")).strip()
    if image_name:
        if Path(image_name).name != image_name:
            raise ValueError(f"题号 {number} 的图片名含路径: {image_name!r}")
        if Path(image_name).suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
            raise ValueError(f"题号 {number} 的图片格式不受支持: {image_name!r}")
        if not (images_dir / image_name).is_file():
            raise FileNotFoundError(f"题号 {number} 缺少图片: {image_name}")

    return [
        number,
        type_label,
        plain_text(record.get("question")),
        image_name,
        *options[:8],
        answer_for(record, type_label),
        plain_text(record.get("explanation")),
        "",
    ]


def verify_zip(path: Path, expected_rows: int) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise ValueError(f"ZIP CRC 校验失败: {bad_member}")
        names = archive.namelist()
        csv_names = [name for name in names if name.lower().endswith(".csv")]
        if csv_names != ["题目.csv"]:
            raise ValueError(f"ZIP 必须且只能包含一个题目.csv，实际: {csv_names}")
        csv_text = archive.read("题目.csv").decode("utf-8-sig")
        rows = list(csv.reader(csv_text.splitlines()))
        if not rows or rows[0] != HEADERS:
            raise ValueError("CSV 表头与题屿格式不一致")
        data_rows = rows[1:]
        if len(data_rows) != expected_rows:
            raise ValueError(f"CSV 题数 {len(data_rows)} 与来源 {expected_rows} 不一致")
        if any(len(row) != len(HEADERS) for row in data_rows):
            raise ValueError("CSV 存在列数不等于 15 的记录")
        if [int(row[0]) for row in data_rows] != list(range(1, expected_rows + 1)):
            raise ValueError("题号不是从 1 开始的连续数字")

        type_counts = Counter(row[1] for row in data_rows)
        invalid_types = set(type_counts) - ALLOWED_TYPES
        if invalid_types:
            raise ValueError(f"存在无效题型: {sorted(invalid_types)}")
        image_refs = {
            item
            for row in data_rows
            for item in row[3].split("|")
            if item
        }
        zip_images = {name.removeprefix("images/") for name in names if name.startswith("images/") and not name.endswith("/")}
        if image_refs != zip_images:
            missing = sorted(image_refs - zip_images)[:10]
            orphaned = sorted(zip_images - image_refs)[:10]
            raise ValueError(f"图片引用不一致，缺少={missing}，未引用={orphaned}")

        for row in data_rows:
            number, type_label = int(row[0]), row[1]
            text_fields = [row[2], *row[4:12], row[13]]
            if any(HTML_TAG.search(value) for value in text_fields):
                raise ValueError(f"题号 {number} 仍含 HTML 标签")
            if type_label == "判断" and (row[4], row[5], row[12]) not in {
                ("对", "错", "对"),
                ("对", "错", "错"),
            }:
                raise ValueError(f"题号 {number} 的判断题格式无效")
            if type_label == "单选" and not re.fullmatch(r"[A-H]", row[12]):
                raise ValueError(f"题号 {number} 的单选答案格式无效")
            if type_label == "多选" and not re.fullmatch(r"[A-H]{2,8}", row[12]):
                raise ValueError(f"题号 {number} 的多选答案格式无效")

        return {
            "total": len(data_rows),
            "type_counts": dict(type_counts),
            "image_questions": sum(bool(row[3]) for row in data_rows),
            "image_files": len(zip_images),
            "members": len(names),
        }


def build(source_json: Path, images_dir: Path, output_zip: Path) -> dict[str, Any]:
    source = json.loads(source_json.read_text(encoding="utf-8"))
    records = source.get("questions")
    if not isinstance(records, list) or not records:
        raise ValueError("来源 JSON 中没有 questions 数组")
    output_zip.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="tiyu_km1_", dir=output_zip.parent) as temp_name:
        temp_dir = Path(temp_name)
        csv_path = temp_dir / "题目.csv"
        rows = [record_to_row(number, record, images_dir) for number, record in enumerate(records, 1)]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
            writer.writerow(HEADERS)
            writer.writerows(rows)

        referenced_images = sorted({row[3] for row in rows if row[3]})
        temp_zip = temp_dir / "题屿科目一题库.zip"
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            archive.write(csv_path, "题目.csv")
            for image_name in referenced_images:
                archive.write(images_dir / image_name, f"images/{image_name}")
        shutil.copyfile(temp_zip, output_zip)

    return verify_zip(output_zip, len(records))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成题屿可导入题库 ZIP")
    parser.add_argument("--source", type=Path, required=True, help="抓取后的 questions JSON")
    parser.add_argument("--images", type=Path, required=True, help="题目图片目录")
    parser.add_argument("--output", type=Path, required=True, help="输出 ZIP")
    args = parser.parse_args()
    summary = build(args.source.resolve(), args.images.resolve(), args.output.resolve())
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
