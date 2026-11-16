"""Versioned report-writing instructions and immutable effective-prompt bindings."""
import json
from .storage import RecoveryError
from .verification import digest

EXTRA = {
    "generation": """报告写作合同 v2（优先于上文通用章节建议）：
按用户问题组织简短正文；未要求比较时不强加“比较依据”章节，不为凑模板引入任务无关断言。
每行只写一个证据支持范围内的事实并紧跟对应引用；同一行的每个事实都必须被该引用支持。
不要把来源文件名、输入文件数量、“唯一来源”等运行元信息与产品事实共用一条引用。
用户未要求在正文讨论资料范围时，不额外罗列这些元信息，引用来源由应用附录追溯；
用户明确要求时，应独立说明它属于输入范围信息，不能用资料正文摘录冒充对输入范围的证明。
建议中的事实前提仍需证据：先单独陈述有引用的已知事实，再写纯建议；不要把“免维护”等未确认能力包装成建议。
写“推断”“建议”“未验证”不能让事实免于举证。保留必要比较和限制，不能为提分删掉用户需求。
资料不足只针对当前问题说明尚不能确认的结论，不扩写任务无关的缺失能力清单；未提及不等于不存在。
这些是写作约束，不要把约束、引用规则、同义改写说明或评分解释抄进报告。
不要输出自行生成的Evidence Ledger或代码围栏来隐藏未核查内容。
""",
    "repair": """修订写作合同 v2（以下约束你的修改方式，不是报告内容）：
直接改有问题的原句：拆分事实、补上对应证据，或收窄任务不需要且没有支持的额外断言。
不要新增“这些事实来自同一来源”“任意摘录不能证明所有结论”“这是语言同义改写”等解释段落。
用户未要求时，不在正文添加文件名、来源数量或唯一性；用户明确要求的范围信息必须保留并与产品事实分开，不能用产品摘录证明它。
纯建议与可核查前提分行，事实前提仍要引用；不要因标成“建议/推断”就隐去事实。
不要为凑章节增加新的比较、机制断言或无关证据缺口，也不要通过删掉任务要求提高分数。
只输出围绕原问题的完整修订报告，不输出评分说明、修改说明或引用教学。
""",
}


def policy_spec(version="v1"):
    if version not in {"v1", "v2"}:
        raise RecoveryError("Unknown report-writing policy version.")
    return dict(version=version, generation_sha256=digest(EXTRA["generation"] if version == "v2" else ""),
                repair_sha256=digest(EXTRA["repair"] if version == "v2" else ""))


def validate_spec(spec):
    if not isinstance(spec, dict) or spec != policy_spec(spec.get("version")):
        raise RecoveryError("Report-writing policy does not match this version's instruction hashes.")
    return spec["version"]


def prompt_text(base, stage, spec=None):
    if stage not in EXTRA:
        raise RecoveryError("Unknown report-writing prompt stage.")
    version = validate_spec(spec) if spec is not None else "v1"
    return base if version == "v1" else base + "\n" + EXTRA[stage]


def bound_prompt(ledger, base, stage):
    path = ledger.directory / "run.json"
    manifest = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    spec = manifest.get("report_policy")
    prompt = prompt_text(base, stage, spec)
    with ledger.store.transaction() as connection:
        row = connection.execute("SELECT payload FROM records WHERE kind='report_policy' AND id='singleton'").fetchone()
        if spec is None:
            if row is not None:
                raise RecoveryError("A pinned report policy cannot be removed from the manifest.")
            return prompt  # Legacy runs keep their original v1 behavior, without invented history.
        if row is None:
            if any(json.loads(event[0]).get("event") == "model_started" for event in connection.execute("SELECT payload FROM events")):
                raise RecoveryError("Cannot change an unversioned run into a newly versioned report policy after model calls.")
            connection.execute("INSERT INTO records VALUES ('report_policy','singleton',?)", (json.dumps(spec, sort_keys=True),))
        elif json.loads(row[0]) != spec:
            raise RecoveryError("Report policy differs from the pinned run policy.")
        expected = dict(stage=stage, prompt_sha256=digest(prompt))
        row = connection.execute("SELECT payload FROM records WHERE kind='report_prompt' AND id=?", (stage,)).fetchone()
        if row is None:
            connection.execute("INSERT INTO records VALUES ('report_prompt',?,?)", (stage, json.dumps(expected, sort_keys=True)))
        elif json.loads(row[0]) != expected:
            raise RecoveryError("Effective report prompt changed; refusing silent prompt replacement during resume.")
    return prompt
