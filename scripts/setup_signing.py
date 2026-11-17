#!/usr/bin/env python3
"""校验现有固定签名 keystore，并写入 GitHub Actions Secrets。

用法（在仓库根目录或任意位置执行）:
    export GITHUB_TOKEN=<你的GitHub Token>   # 具备 repo / secrets 写权限
    pip install pynacl                 # 依赖
    python3 setup_signing.py

可配置环境变量:
    GITHUB_TOKEN       GitHub 访问令牌（必填，不落盘、不硬编码）
    DQ_GITHUB_REPO     目标仓库，默认 dadaluo/TiYu
    DQ_KEYSTORE_DIR    keystore 本地保存目录，默认仓库根目录下 keystore（仓库外请自行指定）
    DQ_KEYSTORE_FILE   已有固定 keystore 文件路径（必须存在）

行为:
    1. 只复用已有 keystore 与 pass.txt，文件缺失时停止，不生成新密钥。
    2. 用参数数组与环境变量传密码，按 gradle.properties 校验固定证书。
    3. 经仓库公钥 sealed box 加密，写入 DQ_KEYSTORE_B64 / DQ_KS_STORE_PASS。
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from nacl import encoding, public

PAT = os.environ.get("GITHUB_TOKEN", "")
REPO = os.environ.get("DQ_GITHUB_REPO", "dadaluo/TiYu")
ROOT = Path(__file__).resolve().parent.parent
KS_DIR = os.environ.get("DQ_KEYSTORE_DIR", str(ROOT / "keystore"))
KS_FILE = os.environ.get("DQ_KEYSTORE_FILE", os.path.join(KS_DIR, "dronequiz.keystore"))
SIGNING = dict(
    line.strip().split("=", 1)
    for line in (ROOT / "gradle.properties").read_text(encoding="utf-8").splitlines()
    if line.startswith("tiyu.signing.")
)
ALIAS = SIGNING["tiyu.signing.keyAlias"]
CERTIFICATE_SHA256 = SIGNING["tiyu.signing.certificateSha256"]
PASS_FILE = os.path.join(KS_DIR, "pass.txt")

if not PAT:
    sys.exit("[!] 请先 export GITHUB_TOKEN=<你的GitHub Token>")


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)
    if r.returncode != 0:
        print("[FAIL]", cmd[0], "执行失败，请检查密钥和配置；敏感参数不输出。")
        sys.exit(1)
    return r.stdout.strip()


def api(method, path, payload=None):
    url = f"https://api.github.com{path}"
    data = None
    headers = {
        "Authorization": f"Bearer {PAT}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "dronequiz-setup",
    }
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            txt = resp.read().decode()
            return resp.status, (txt and json.loads(txt) or {})
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:500]


def main():
    os.makedirs(KS_DIR, exist_ok=True)

    # 本项目只复用既定固定密钥，缺失时停止，禁止生成另一把同名密钥。
    if not os.path.isfile(KS_FILE) or not os.path.isfile(PASS_FILE):
        sys.exit("[FAIL] 缺少固定 keystore 或 pass.txt，请从备份恢复，不要重新生成。")
    password = Path(PASS_FILE).read_text(encoding="utf-8").strip()
    if not password:
        sys.exit("[FAIL] 固定签名密码为空。")
    keytool_env = dict(os.environ, DQ_SIGNING_PASSWORD=password)
    certificate_pem = sh([
        "keytool", "-exportcert", "-rfc", "-keystore", KS_FILE,
        "-storepass:env", "DQ_SIGNING_PASSWORD", "-alias", ALIAS,
    ], env=keytool_env)
    certificate_body = re.search(
        r"-----BEGIN CERTIFICATE-----([\s\S]+?)-----END CERTIFICATE-----",
        certificate_pem,
    )
    if not certificate_body:
        sys.exit("[FAIL] 无法读取固定签名证书。")
    fingerprint = hashlib.sha256(base64.b64decode(certificate_body.group(1))).hexdigest()
    if fingerprint != CERTIFICATE_SHA256:
        sys.exit("[FAIL] 证书与 gradle.properties 中的固定签名不一致，拒绝上传 Secrets。")
    print("[i] 固定签名证书校验通过")

    # 3. GitHub 仓库公钥
    status, pk = api("GET", f"/repos/{REPO}/actions/secrets/public-key")
    if status != 200:
        print("获取公钥失败:", status, pk)
        sys.exit(1)
    print(f"[i] 仓库公钥 key_id = {pk['key_id']}")

    pk_obj = public.PublicKey(pk["key"].encode(), encoding.Base64Encoder())
    sealed = public.SealedBox(pk_obj)

    ks_b64 = base64.b64encode(open(KS_FILE, "rb").read()).decode()

    secrets = {
        "DQ_KEYSTORE_B64": ks_b64,
        "DQ_KS_STORE_PASS": password,
    }
    for name, raw in secrets.items():
        enc = sealed.encrypt(raw.encode())
        status, resp = api(
            "PUT",
            f"/repos/{REPO}/actions/secrets/{name}",
            {"encrypted_value": base64.b64encode(enc).decode(), "key_id": pk["key_id"]},
        )
        print(f"[{'OK' if status in (201, 204) else 'FAIL'}] secret {name}: HTTP {status} {resp if status not in (201, 204) else ''}")

    print("[DONE] keystore 与 secrets 就绪")
    print(f"[!!] 请立即异地备份 {KS_FILE} 与密码（丢失将无法覆盖安装升级）")


if __name__ == "__main__":
    main()
