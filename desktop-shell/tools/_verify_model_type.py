# -*- coding: utf-8 -*-
"""验证模型名单分流的快速自检（不依赖网络，用临时目录模拟真实数据流）。

覆盖：
  1) split_models_by_service：model_type 权威 > 名称启发式兜底；
  2) 4=重排序确定性丢弃（含名字无线索的模型）；
  3) 0/缺失/非法值回退名称启发式（旧平台行为不变）;
  4) fetch_relay_models 解析契约（file:// 本地数据，零网络）；
  4.5) 业务令牌：严格按契约取 userinfo.ai_token（无 token 响应回退）；
  5) ensure_tokengine_catalog 全链路：各服务 profile 按类型落位。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # desktop-shell（desktop 包）

from desktop.auth import config as cfg
from desktop.auth.catalog import ensure_tokengine_catalog, split_models_by_service
from desktop.auth.client import OAuthError, fetch_relay_models
from desktop.auth.manager import AuthManager

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  -> {detail}" if detail else ""))
    if not cond:
        failures.append(name)


MODELS = [
    "deepseek-ai/DeepSeek-V4-Flash-0731",   # 1 文生文
    "glm-4v-image-understand",              # 1 文生文（名字带 image，启发式会错归）
    "my-custom-vector-model",               # 5 向量（名字无线索，只有类型能救）
    "secret-rerank-model-x",                # 4 重排序（名字无线索，只有类型能丢弃）
    "seedream-4.0",                         # 2 文生图
    "seedance-pro",                         # 3 文生视频
    "BAAI/bge-m3",                          # 5 向量
]
MODEL_TYPES = {
    "deepseek-ai/DeepSeek-V4-Flash-0731": 1,
    "glm-4v-image-understand": 1,
    "my-custom-vector-model": 5,
    "secret-rerank-model-x": 4,
    "seedream-4.0": 2,
    "seedance-pro": 3,
    "BAAI/bge-m3": 5,
}

# ------------------------------------------------------------------ #
# 1) model_type 权威分流
# ------------------------------------------------------------------ #
def sn(entries):
    """split 条目的模型名列表（新契约为 [{"name","model_type"}]）。"""
    return [e["name"] for e in entries]


def st(entries):
    """split 条目的 名称->model_type 映射。"""
    return {e["name"]: e.get("model_type") for e in entries}


split = split_models_by_service(MODELS, MODEL_TYPES)
check("文生文(1) -> llm", sn(split["llm"]) == ["deepseek-ai/DeepSeek-V4-Flash-0731",
      "glm-4v-image-understand"], str(split["llm"]))
check("task 恒为空（1.6.11：task 留空 = inherit 跟随对话模型，不再双挂载）",
      split["task"] == [], str(split["task"]))
check("llm 每条带 model_type=1",
      all(mt == 1 for mt in st(split["llm"]).values()), str(st(split["llm"])))
check("名字带 image 但标注 1 不被启发式错归 imagegen",
      "glm-4v-image-understand" not in sn(split["imagegen"]), str(split["imagegen"]))
check("向量(5) -> embedding（含无线索名）",
      sn(split["embedding"]) == ["my-custom-vector-model", "BAAI/bge-m3"],
      str(split["embedding"]))
check("embedding 每条带 model_type=5",
      all(mt == 5 for mt in st(split["embedding"]).values()), str(st(split["embedding"])))
check("文生图(2) -> imagegen", sn(split["imagegen"]) == ["seedream-4.0"], str(split["imagegen"]))
check("imagegen 带 model_type=2", st(split["imagegen"]).get("seedream-4.0") == 2, "")
check("文生视频(3) -> videogen", sn(split["videogen"]) == ["seedance-pro"], str(split["videogen"]))
check("videogen 带 model_type=3", st(split["videogen"]).get("seedance-pro") == 3, "")
all_names = [n for entries in split.values() for n in sn(entries)]
check("重排序(4) 确定性丢弃（名字无线索也丢）",
      "secret-rerank-model-x" not in all_names, str(split))

# ------------------------------------------------------------------ #
# 2) 未标注(0)/缺失/非法 -> 保守归对话（无名称兜底）
# ------------------------------------------------------------------ #
split0 = split_models_by_service(
    ["BAAI/bge-m3", "seedance-pro", "totally-unknown-chat"],
    {"BAAI/bge-m3": 0, "seedance-pro": 99, "totally-unknown-chat": None},
)
check("0=未标注归对话", "BAAI/bge-m3" in sn(split0["llm"]), str(split0["llm"]))
check("未知值(99)归对话", "seedance-pro" in sn(split0["llm"]), str(split0["llm"]))
check("None 值归对话", "totally-unknown-chat" in sn(split0["llm"]), str(split0["llm"]))
check("未知类型落 model_type=1",
      st(split0["llm"]).get("totally-unknown-chat") == 1, "")

# ------------------------------------------------------------------ #
# 3) 不传 model_types：所有模型归对话（无名称分流）
# ------------------------------------------------------------------ #
legacy = split_models_by_service(["BAAI/bge-m3", "seedance-pro", "gpt-4o"])
check("无类型时全部归对话",
      sn(legacy["llm"]) == ["BAAI/bge-m3", "seedance-pro", "gpt-4o"], str(legacy))
check("无类型时 embedding/imagegen/videogen 为空",
      legacy["embedding"] == [] and legacy["imagegen"] == [] and legacy["videogen"] == [],
      str(legacy))
check("无类型对话带 model_type=1",
      all(mt == 1 for mt in st(legacy["llm"]).values()), str(st(legacy["llm"])))

# ------------------------------------------------------------------ #
# 3.5) 平台全标 1（旧事故）：纯 model_type 分流下全部归对话
#      （平台已修复 model_type，此场景仅作回归保护）
# ------------------------------------------------------------------ #
bad_platform = {
    "MiniMax/MiniMax-M2.7": 1,
    "Qwen/Qwen3-VL-8B-Instruct": 1,
    "Qwen/Qwen3-Reranker-8B": 1,
    "Qwen/Qwen3-Embedding-8B": 1,
}
split_bad = split_models_by_service(list(bad_platform), bad_platform)
sn_bad = {s: sn(e) for s, e in split_bad.items()}
check("全标1：全部归对话（含原 Reranker/Embedding）",
      sn_bad["llm"] == list(bad_platform), str(sn_bad["llm"]))
check("全标1：embedding/imagegen/videogen 为空",
      split_bad["embedding"] == [] and split_bad["imagegen"] == []
      and split_bad["videogen"] == [], str(sn_bad))

# ------------------------------------------------------------------ #
# 4) fetch_relay_models 解析契约（file:// 本地数据，零网络）
#    模型名单唯一来源 = 中继 /v1/models（2026-09-24 起，无 userinfo 兜底）
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    payload = {"data": [
        {"id": "a/chat-1", "model_type": 1},
        {"id": "b/embed-1", "model_type": 5},
        {"id": "c/rerank-1", "model_type": 4},
        {"id": "no-type"},
        "bare-name",
        {"model": "d/alt-field", "model_type": 2.0},
        {"id": "  a/chat-1  "},   # 重复：去重保序
        {"id": ""},               # 空名：跳过
        42,                       # 非法项：跳过
    ]}
    models_file = Path(td) / "models"
    models_file.write_text(json.dumps(payload), encoding="utf-8")
    names, mts = fetch_relay_models(Path(td).as_uri(), "sk-test")
    check("解析 id/model/model_name 字段并去重保序",
          names == ["a/chat-1", "b/embed-1", "c/rerank-1", "no-type",
                    "bare-name", "d/alt-field"], str(names))
    check("model_type 归一 int（含 float 2.0）",
          mts == {"a/chat-1": 1, "b/embed-1": 5, "c/rerank-1": 4,
                  "d/alt-field": 2}, str(mts))

    empty_dir = Path(td) / "empty"
    empty_dir.mkdir()
    (empty_dir / "models").write_text(json.dumps({"data": []}), encoding="utf-8")
    try:
        fetch_relay_models(empty_dir.as_uri(), "sk-test")
        check("空名单必须抛错（防网关抖动清空 catalog）", False, "no exception")
    except OAuthError:
        check("空名单必须抛错（防网关抖动清空 catalog）", True)

    bad_dir = Path(td) / "bad"
    bad_dir.mkdir()
    (bad_dir / "models").write_text("not-json", encoding="utf-8")
    try:
        fetch_relay_models(bad_dir.as_uri(), "sk-test")
        check("非法 JSON 必须抛错", False, "no exception")
    except OAuthError:
        check("非法 JSON 必须抛错", True)

    # _derive 瘦身后只管账号与中继推导（模型解析已迁至 fetch_relay_models）
    mgr = AuthManager(root=Path(td) / "root", home=Path(td) / "home")
    phone, relay, src = mgr._derive({"phone": "15512348602"})
    check("_derive 返回 (phone, relay_base, relay_source)",
          phone == "15512348602" and relay and src,
          f"{phone} / {relay} / {src}")

# ------------------------------------------------------------------ #
# 4.5) 业务令牌：严格按契约取 userinfo.ai_token（无 token 响应回退）
# ------------------------------------------------------------------ #
pick_token = lambda account: str(account.get(cfg.USERINFO_AI_TOKEN_FIELD) or "")
check("契约：ai_token 从 userinfo 下发", pick_token({"ai_token": "sk-new"}) == "sk-new")
check("缺失 ai_token -> 登录报 no_token", pick_token({}) == "")

# ------------------------------------------------------------------ #
# 5) ensure_tokengine_catalog 全链路：各服务 profile 按类型落位
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "workspace"
    ensure_tokengine_catalog(
        home=home, api_key="sk-test-token",
        base_url="https://tokengine.hanyoai.com/v1",
        models=MODELS, model_types=MODEL_TYPES,
    )
    cat = json.loads((home / "data/user/settings/model_catalog.json").read_text(
        encoding="utf-8"))
    tok_profiles = lambda svc: [
        p for p in cat["services"][svc]["profiles"]
        if p.get("connection_id") == "tokengine"
    ]
    names = lambda svc: [m["model"] for p in cat["services"][svc]["profiles"]
                         if p.get("connection_id") == "tokengine"
                         for m in (p.get("models") or [])]
    types_of = lambda svc: [m.get("model_type") for p in cat["services"][svc]["profiles"]
                            if p.get("connection_id") == "tokengine"
                            for m in (p.get("models") or [])]
    check("catalog llm 按类型落位", names("llm") == [
        "deepseek-ai/DeepSeek-V4-Flash-0731", "glm-4v-image-understand"], str(names("llm")))
    check("catalog llm 托管 profile 已挂接 connection",
          all(p.get("connection_id") == "tokengine" for p in tok_profiles("llm")),
          str(tok_profiles("llm")))
    check("catalog llm 落库带 model_type=1", types_of("llm") == [1, 1], str(types_of("llm")))
    check("catalog embedding 落位", names("embedding") == [
        "my-custom-vector-model", "BAAI/bge-m3"], str(names("embedding")))
    check("catalog embedding 落库带 model_type=5",
          types_of("embedding") == [5, 5], str(types_of("embedding")))
    check("catalog imagegen 落位", names("imagegen") == ["seedream-4.0"], str(names("imagegen")))
    check("catalog videogen 落位", names("videogen") == ["seedance-pro"], str(names("videogen")))
    check("catalog task 无 tokengine profile（不再双挂载）",
          tok_profiles("task") == [], str(tok_profiles("task")))
    check("重排序模型未写入任何服务",
          "secret-rerank-model-x" not in sum((names(s) for s in
                                              ("llm", "task", "embedding", "imagegen", "videogen")), []),
          "")

# ------------------------------------------------------------------ #
# 6) 历史残留清理：1.6.9 时代双挂载写入的 task profile，在新的登录/
#    刷新（ensure 空授权分支）与设置页 Apply（reconcile 不回插 task）下
#    都必须被清掉，提供商页计数随之从 17 回到 9（llm 8 + embedding 1）。
# ------------------------------------------------------------------ #
with tempfile.TemporaryDirectory() as td:
    home = Path(td) / "workspace"
    ensure_tokengine_catalog(
        home=home, api_key="sk-test-token",
        base_url="https://tokengine.hanyoai.com/v1",
        models=MODELS, model_types=MODEL_TYPES,
    )
    # 手工造一份 1.6.9 形状的残留：task profile 带 llm 的复制模型
    cat_path = home / "data/user/settings/model_catalog.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    cat["services"]["task"]["profiles"] = [{
        "id": "task-profile-tokengine-legacy",
        "name": "Tokengine (OpenAI API)",
        "binding": "openai",
        "base_url": "https://tokengine.hanyoai.com/v1",
        "api_key": "sk-test-token",
        "models": [{"id": "t-0", "name": "deepseek-ai/DeepSeek-V4-Flash-0731",
                    "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                    "model_type": 1}],
        "connection_id": "tokengine",
    }]
    cat["services"]["task"]["active_profile_id"] = "task-profile-tokengine-legacy"
    cat_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")

    # 路径 A：再次登录/刷新（ensure 空授权分支摘除 task profile）
    ensure_tokengine_catalog(
        home=home, api_key="sk-test-token",
        base_url="https://tokengine.hanyoai.com/v1",
        models=MODELS, model_types=MODEL_TYPES,
    )
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    check("残留 task profile 被再次登录清掉（ensure 空授权分支）",
          [p for p in cat["services"]["task"]["profiles"]
           if p.get("connection_id") == "tokengine"] == [],
          str(cat["services"]["task"]["profiles"]))

    # 路径 B：设置页整包 Apply（reconcile 以 live 为权威，但 task 不在
    # 托管清单里 → 陈旧 task profile 不会被回插复活）
    # 注：tokengine_reconcile 是纯标准库自包含模块（无相对导入），这里用
    # importlib 按文件路径加载，绕开 deeptutor.services.config.__init__
    # 的重导入链（会级联拉起 tools.builtin → yaml 等本 venv 不需要的依赖）。
    import importlib.util
    _recon_path = (Path(__file__).resolve().parents[2]
                   / "deeptutor" / "services" / "config" / "tokengine_reconcile.py")
    _spec = importlib.util.spec_from_file_location("_tokengine_reconcile", _recon_path)
    _recon = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_recon)
    reconcile_tokengine_catalog_update = _recon.reconcile_tokengine_catalog_update
    proposed = json.loads(cat_path.read_text(encoding="utf-8"))
    proposed["services"]["task"]["profiles"] = []
    merged = reconcile_tokengine_catalog_update(cat, proposed)
    check("残留 task profile 不被 Apply 回插复活（reconcile）",
          [p for p in merged["services"]["task"]["profiles"]
           if p.get("connection_id") == "tokengine"] == [],
          str(merged["services"]["task"]["profiles"]))
    check("reconcile 仍保护 llm 托管 profile（不误伤）",
          any(p.get("connection_id") == "tokengine"
              for p in merged["services"]["llm"]["profiles"]),
          str(merged["services"]["llm"]["profiles"]))

print()
if failures:
    print(f"FAILED: {len(failures)} -> {failures}")
    sys.exit(1)
print("ALL PASS")
