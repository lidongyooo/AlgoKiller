import pytest

from algokiller_harness.prompts import BASE_PROMPT, SUPPORTED_ANALYSIS_MODES, build_system_prompt


def test_supported_modes_are_explicit():
    assert SUPPORTED_ANALYSIS_MODES == ("ciphertext", "general")


def test_base_prompt_stays_mode_neutral():
    assert "ARM64 trace 证据分析系统" in BASE_PROMPT
    assert "算法还原系统" not in BASE_PROMPT
    assert "还原算法或计算过程" not in BASE_PROMPT
    assert "最终 Python 还原源码" not in BASE_PROMPT
    assert "标准算法只有在 trace 证据充分时才能使用第三方库" not in BASE_PROMPT
    assert "当任务需要源码 artifact 时" in BASE_PROMPT
    assert "是否继续追踪/是否继续分析" in BASE_PROMPT
    assert "自动上下文压缩" in BASE_PROMPT
    assert "外部 note agent 会自动扫描当前上下文" in BASE_PROMPT
    assert "system prompt 加一条由阶段性笔记组成的 user prompt" in BASE_PROMPT
    assert "trace_context 抽查最重要的 1-3 个 line/address/register/memory/call/hexdump/ret 锚点" in BASE_PROMPT
    assert "不存在可手动调用的 note 读写工具" in BASE_PROMPT


def test_ciphertext_prompt_contains_mode_goal():
    prompt = build_system_prompt("ciphertext")

    assert "当前分析模式：给定密文追溯还原加密算法与明文" in prompt
    assert "目标密文完整值" in prompt
    assert "ask_user 调用会先经过验收 agent" in prompt
    assert "每次调用 trace_search 必须显式携带 limit" in prompt
    assert "from_line 与 before_line 中选择一个" in prompt
    assert "before_line 只搜索该行之前的内容并按最近命中优先返回" in prompt
    assert "trace_all_search 只接受 query 和 limit" in prompt
    assert "trace_all_search 的返回只表示目标内容在哪些文件中出现过" in prompt
    assert "不要依据返回行号先后、第一条命中或不同 file_id 之间的相对顺序判断目标内容最早的生成位置" in prompt
    assert "寻找写入该地址/内容的 `mem_w`" in prompt
    assert "调用函数通过 dst/ret/hexdump/参数填充目标内容地址的证据" in prompt
    assert "trace_search 和 trace_context 的条数参数最大值都是 100" in prompt
    assert "搜索 hex/字节数据时必须按字节处理" in prompt
    assert "当某次字节数据原序搜索未命中时，必须尝试 endian 反序再搜索一次" in prompt
    assert "如果待搜索字节超过 4 字节" in prompt
    assert "不要只固定使用开头 4 字节" in prompt
    assert "每轮 trace_search 前先明确本轮搜索目的" in prompt
    assert "默认选择 2-4 个高辨识度窗口" in prompt
    assert "最终交付必须匹配用户任务" in prompt
    assert "交付已确认部分、合理高置信推断和未确认缺口" in prompt
    assert "是否继续追踪/是否继续分析" in prompt
    assert "用户一般情况下只会提供目标密文本体" in prompt
    assert "字段名或用途、请求路径、编码形式、header/body 语义、样本、时间戳、设备参数等都只是附加信息" in prompt
    assert "默认用户只提供目标密文完整值，这是本模式的正常输入，不是信息不足" in prompt
    assert "本模式启动时只要求目标密文本体" in prompt
    assert "不要把“用户不知道字段名”当作阻塞条件" in prompt
    assert "不要因为用户没有提供更多上下文就调用 ask_user" in prompt
    assert "只有在目标密文本身缺失" in prompt
    assert "能解释批量读写/转换的 `call func` 边界" in prompt
    assert "`call func: name(args)`、它后面的 hexdump、以及对应 `ret: value` 是一级证据源" in prompt
    assert "一次调用中完成大量 `mem_r`/`mem_w`" in prompt
    assert "把该调用记录为一次批量读写或批量变换候选" in prompt
    assert "不要只在附近机械搜索单条 `mem_w` 而忽略 call 本身已经暴露的输入输出关系" in prompt
    assert "严格还原以左侧 hex bytes、address 和 length 为准" in prompt
    assert "字符串密文：首先用原始字符串完整搜索" in prompt
    assert "只有在用户额外提供字段名时，才把字段名作为辅助线索搜索" in prompt
    assert "二进制密文：首先把抓包字节按 hexdump 左侧格式搜索" in prompt
    assert "`08 d2 11`" in prompt
    assert "`08d211`" in prompt
    assert "`11 d2 08`" in prompt
    assert "`11d208`" in prompt
    assert "对每个候选片段同时搜索原序和反序" in prompt
    assert "2-4 个高辨识度的 4 字节滑动片段搜索" in prompt
    assert "只把它当作存在性证据：选择候选 file_id" in prompt
    assert "不比较不同 file_id 的行号或返回顺序来判断全局最早生成点" in prompt
    assert "优先取最前面的可信命中作为“最早可见点”候选" in prompt
    assert "不是直接当作最终生成点" in prompt
    assert "最终输出/上报前生成点" in prompt
    assert "最近上游写入点" in prompt
    assert "最早命中只是候选，不是结论" in prompt
    assert "常量、输入、旧数据、消费点或冲突命中" in prompt
    assert "用 `before_line` 搜索 `mem_w=0x...`" in prompt
    assert "优先选择 before_line 返回的最近命中" in prompt
    assert "定位最近的生成 call 或 mem_w 后，不要机械地无限循环向上追踪" in prompt
    assert "有限回溯规则用于确认数据流属于目标密文链条" in prompt
    assert "再进入算法识别、候选比对和验证阶段" in prompt
    assert "默认只对“这次生成/写入的数据”做一次高质量搜索" in prompt
    assert "3 字节或 4 字节片段搜索" in prompt
    assert "只搜 1 字节或 2 字节" in prompt
    assert "搜索结果最上面的一条不可信" in prompt
    assert "不要把每个来源寄存器、内存地址都展开成无界回溯" in prompt
    assert "优先按 4 字节分组滑动尝试" in prompt
    assert "默认截取 2-4 个有辨识度的 4 字节窗口" in prompt
    assert "才继续换窗口或扩展到 5-8 字节" in prompt
    assert "真正能定位核心算法的片段可能在中间或后面的块" in prompt
    assert "先从指令形态和数据形态建立算法假设" in prompt
    assert "分组变换、流式变换、hash/MAC/signature" in prompt
    assert "证据约束的候选穷举" in prompt
    assert "覆盖所有与这些硬特征相容的基础算法族" in prompt
    assert "分组密码/模式、流密码或 PRNG keystream、hash/MAC、CRC/checksum" in prompt
    assert "匹配/冲突矩阵" in prompt
    assert "没有证据的候选只能保留为未确认，不能提升为结论" in prompt
    assert "最相似基础算法 + 已确认差异" in prompt
    assert "不要因为存在魔改就退化成“自定义加密”" in prompt
    assert "基础算法候选比对结果" in prompt
    assert "与 trace 硬特征相容且已比较的候选族" in prompt
    assert "`intermediate op secret = output`" in prompt
    assert "只需先完整扣出一个代表性轮/迭代" in prompt
    assert "优先判断是哪一小步被替换或魔改" in prompt
    assert "用相邻块/字段关系验证" in prompt
    assert "只扣关键差异函数、常量表、表查找规则和数据依赖" in prompt
    assert "module_base = abs_address - relative_address" in prompt
    assert "局部实现 -> 中间值验证 -> 扩展实现" in prompt
    assert "算法深挖补充规则" in prompt
    assert "默认不要超过 3 层，超过前必须先用中间值或上下文验证当前链条仍然可信" in prompt
    assert "增量可作为块大小候选" in prompt
    assert "在加密循环首次执行之前搜索 ctx/key 指针的 `mem_w`" in prompt
    assert "从最终参与密文写入的 eor/add/sub/orr/and/表合并指令向前追 2-3 步" in prompt
    assert "常量匹配是高价值候选证据，但不能替代数据流验证" in prompt
    assert "按原序和字节反序回 trace 搜索" in prompt
    assert "不要试图完整解释 dispatcher、opcode 解码或无关控制流" in prompt
    assert "完整还原交付前建立验证闭环" in prompt
    assert "标准算法只有在 trace 证据充分时才能使用第三方库" in prompt
    assert "证据足以复现时，使用 write_recovered_source 写入 Python 还原源码" in prompt
    assert "只能还原部分时，交付局部源码/伪代码、已确认流程和缺口" in prompt
    assert "AES/类 AES 假设" not in prompt
    assert "CBC 通常表现为第一块明文先与 IV 异或" not in prompt


def test_general_prompt_contains_open_ended_trace_analysis_rules():
    prompt = build_system_prompt("general")

    assert "当前分析模式：通用 trace 证据分析" in prompt
    assert "数据字段含义分析" in prompt
    assert "程序执行流分析" in prompt
    assert "程序检测点分析" in prompt
    assert "不要默认要求写 Python 源码" in prompt
    assert "只有算法复现、生成过程复现或用户明确要求代码时才写源码" in prompt
    assert "每次调用 trace_search 必须显式携带 limit" in prompt
    assert "from_line 与 before_line 中选择一个" in prompt
    assert "每次 trace_search 先确定单一目的" in prompt
    assert "遇到 call/hexdump/ret 时优先解析调用边界" in prompt
    assert "不要把 hexdump 右侧 ASCII 当作字段边界" in prompt
    assert "严格解析必须以左侧 hex bytes、address 和 length 为准" in prompt
    assert "`08 d2 11`" in prompt
    assert "`08d211`" in prompt
    assert "`11 d2 08`" in prompt
    assert "`11d208`" in prompt
    assert "2-4 个高辨识度的 4 字节窗口" in prompt
    assert "字段语义必须分级标注：已确认、高置信推断、未确认" in prompt
    assert "字段边界确认" in prompt
    assert "业务语义推断" in prompt
    assert "按时间顺序列出关键节点" in prompt
    assert "比较指令、参与寄存器/内存值、跳转是否发生" in prompt
    assert "区分“采集字段”“计算中间状态”“判断条件”“命中后的动作”" in prompt
    assert "不要把采集点误判为检测点" in prompt
    assert "字段表、执行流时间线、检测点清单" in prompt
    assert "只有在任务需要代码时，才使用 write_recovered_source" in prompt


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        build_system_prompt("unknown")


def test_removed_dataflow_mode_is_rejected():
    with pytest.raises(ValueError):
        build_system_prompt("dataflow")
