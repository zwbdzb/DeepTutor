# -*- coding: utf-8 -*-
"""端到端确认（真实网关数据，token 只在本地使用、输出打码）。

流程：
  1) 从实况 catalog 读 tokengine connection（base_url + sk- 业务令牌）；
  2) 真实调用中继 /v1/models 拉模型名单；
  3) split_models_by_service 分流，打印各服务归属；
  4) 临时 home 上 ensure_tokengine_catalog 全新写入 → 断言目标形状
     （llm=文生文数 / task=0 / embedding+imagegen+videogen 按类型落位，
     与网关名单动态一致，不硬编码 8/1）；
  5) 把实况 catalog（含 1.6.9 task 残留）拷进另一临时 home，跑一次
     ensure 模拟"再次登录/刷新" → 断言 task 残留被摘除、llm/embedding
     原样保留；
  6) 用 importlib 加载 tokengine_reconcile（纯标准库自包含模块），把
     实况 catalog 过一遍 reconcile（proposed 清空 task）→ 断言不回插。
"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop.auth.catalog import ensure_tokengine_catalog, split_models_by_service
from desktop.auth.client import fetch_relay_models

LIVE = Path(r"C:\Users\frank\EduBuddy\data\user\settings\model_catalog.json")
failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# 1) 实况 connection（不打印完整密钥）
cat = json.loads(LIVE.read_text(encoding="utf-8"))
conn = next(c for c in cat["connections"] if c.get("id") == "tokengine")
base = conn["base_url"].rstrip("/")
key = conn["api_key"]
print(f"relay_base = {base}")
print(f"api_key    = {key[:6]}***{key[-4:]}")

# 2) 真实网关拉模型名单
names, mts = fetch_relay_models(base, key, timeout=15.0)
print(f"\n/v1/models 返回 {len(names)} 个模型:")
for n in names:
    print(f"  - {n}  (model_type={mts.get(n)})")

# 3) 分流
split = split_models_by_service(names, mts)
print("\n分流结果:")
for svc in ("llm", "task", "embedding", "imagegen", "videogen"):
    print(f"  {svc:10s}: {[e['name'] for e in split[svc]]}")
check("task 分流恒为空", split["task"] == [], str(split["task"]))

expected_llm = {e["name"] for e in split["llm"]}
expected_emb = {e["name"] for e in split["embedding"]}
expected_img = {e["name"] for e in split["imagegen"]}
expected_vid = {e["name"] for e in split["videogen"]}

# 4) 临时 home 全新写入
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "fresh"
    ensure_tokengine_catalog(home=home, api_key=key, base_url=base,
                             models=names, model_types=mts)
    c = json.loads((home / "data/user/settings/model_catalog.json").read_text(
        encoding="utf-8"))

    def svc_models(s):
        return {m["model"] for p in c["services"][s]["profiles"]
                if p.get("connection_id") == "tokengine"
                for m in (p.get("models") or [])}

    def svc_types(s):
        return {m["model"]: m.get("model_type")
                for p in c["services"][s]["profiles"]
                if p.get("connection_id") == "tokengine"
                for m in (p.get("models") or [])}

    llm, emb, img, vid = (svc_models("llm"), svc_models("embedding"),
                          svc_models("imagegen"), svc_models("videogen"))
    task_profiles = [p for p in c["services"]["task"]["profiles"]
                     if p.get("connection_id") == "tokengine"]
    total = len(llm) + len(emb) + len(img) + len(vid)
    print(f"\n临时 home 新写 catalog: llm={len(llm)} task={len(task_profiles)} "
          f"embedding={len(emb)} imagegen={len(img)} videogen={len(vid)} "
          f"→ 提供商页合计 {total} 行")
    check("全新写入：llm 与分流一致", llm == expected_llm, str(sorted(llm)))
    check("全新写入：task 无 tokengine profile", task_profiles == [],
          str(task_profiles))
    check("全新写入：embedding 与分流一致", emb == expected_emb, str(sorted(emb)))
    check("全新写入：imagegen/videogen 与分流一致",
          img == expected_img and vid == expected_vid,
          f"{sorted(img)} / {sorted(vid)}")
    check("全新写入：llm 全部 model_type=1",
          all(v == 1 for v in svc_types("llm").values()), str(svc_types("llm")))
    check("全新写入：embedding 全部 model_type=5",
          all(v == 5 for v in svc_types("embedding").values()),
          str(svc_types("embedding")))

# 5) 实况 catalog（含 1.6.9 残留）→ 一次 ensure 自愈
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "migrate"
    cpath = home / "data/user/settings/model_catalog.json"
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cpath.write_text(LIVE.read_text(encoding="utf-8"), encoding="utf-8")

    before = json.loads(cpath.read_text(encoding="utf-8"))
    before_task = [p for p in before["services"]["task"]["profiles"]
                   if p.get("connection_id") == "tokengine"]
    print(f"\n迁移前实况形状: task tokengine profile={len(before_task)} "
          f"(模型数={sum(len(p.get('models') or []) for p in before_task)})")
    check("迁移前实况确有 1.6.9 task 残留", len(before_task) > 0, "")

    ensure_tokengine_catalog(home=home, api_key=key, base_url=base,
                             models=names, model_types=mts)
    after = json.loads(cpath.read_text(encoding="utf-8"))

    def tk_models(c, s):
        return [m["model"] for p in c["services"][s]["profiles"]
                if p.get("connection_id") == "tokengine"
                for m in (p.get("models") or [])]

    after_task = [p for p in after["services"]["task"]["profiles"]
                  if p.get("connection_id") == "tokengine"]
    after_llm, after_emb = tk_models(after, "llm"), tk_models(after, "embedding")
    print(f"迁移后形状: llm={len(after_llm)} task={len(after_task)} "
          f"embedding={len(after_emb)}")
    check("存量残留：一次 ensure 后 task profile 被摘除", after_task == [],
          str(after_task))
    check("存量残留：llm 8 个模型原样保留",
          set(after_llm) == expected_llm and len(after_llm) == len(expected_llm),
          str(after_llm))
    check("存量残留：embedding 原样保留", set(after_emb) == expected_emb,
          str(after_emb))

# 6) reconcile（设置页 Apply 通路）：proposed 清空 task 也不回插
_spec = importlib.util.spec_from_file_location(
    "_tokengine_reconcile",
    Path(__file__).resolve().parents[2] / "deeptutor" / "services"
    / "config" / "tokengine_reconcile.py")
_recon = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_recon)
proposed = json.loads(LIVE.read_text(encoding="utf-8"))
proposed["services"]["task"]["profiles"] = []
merged = _recon.reconcile_tokengine_catalog_update(
    json.loads(LIVE.read_text(encoding="utf-8")), proposed)
merged_task = [p for p in merged["services"]["task"]["profiles"]
               if p.get("connection_id") == "tokengine"]
check("reconcile：proposed 清空 task 不被回插", merged_task == [],
      str(merged_task))
check("reconcile：llm 托管 profile 仍被保护",
      any(p.get("connection_id") == "tokengine"
          for p in merged["services"]["llm"]["profiles"]), "")

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("E2E ALL PASS")
