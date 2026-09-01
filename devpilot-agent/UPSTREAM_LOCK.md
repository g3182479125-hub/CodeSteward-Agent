# Upstream lock

状态：P0 基线记录，待退出门禁完成后冻结。

由于当前上游目录来自 ZIP 解压且不包含 `.git` 元数据，本文件不伪造 commit。最终 P0 退出前必须补充可验证的来源归档、文件指纹和依赖锁文件。

| 项目 | 用途 | 本地来源 | 版本/状态 |
|---|---|---|---|
| nanobot | 主 Runtime | `../nanobot-inspect/nanobot-main` | `nanobot-ai 0.3.0`；ZIP，无 commit |
| DeepSeek Harness | 架构参考 | `../architecture-eval/deepseek/deepseek-harness-master` | `0.1.2-alpha.2`；Developer Preview；ZIP，无 commit |
| Codex | 架构参考 | `../architecture-eval/codex/codex-main` | ZIP，无 commit；源码以 Rust 为主 |

## 来源归档 SHA-256

| 项目 | 归档 | SHA-256 | 核验日期 |
|---|---|---|---|
| nanobot | `nanobot-main.zip` | `2E001243DB332C293142FC2918C97ABBD4825018BF0BEAE99E60DD7C702D7A75` | 2026-08-31 |
| DeepSeek Harness | `deepseek-harness-master (1).zip` | `73BDAD2A3F431B9110EA67C892D3FAC827AE558A5308640B05577AE145250755` | 2026-08-31 |
| Codex | `codex-main.zip` | `2131151A41A044BB18006C7BA316365EAA92CD43D68B5A905C2B35D2E07CA2C8` | 2026-08-31 |

## 关键文件 SHA-256

| 项目/文件 | SHA-256 |
|---|---|
| nanobot `pyproject.toml` | `C9AC8338E0302116AF793D9DFC874964A7809BD1A4A2E7C2A381ADC11F13E253` |
| nanobot `nanobot/__init__.py` | `00097F56CD8D3E9CF7D6234243E6928C34659825E1D311BD5C05876581F3C9B1` |
| DeepSeek Harness `package.json` | `F00C72C7C70091A0BCCACFCB802B6295A124E237E1C0C16D93E8F0B1F85644A8` |
| Codex `README.md` | `BA4E1F69FF48386E72A9C5E1EDAF76AAD64A475C2D51AF79CCBA6D1128261BA7` |

## 版本锁定规则

1. DevPilot 运行时只能使用经过记录的 nanobot 版本。
2. 上游源码更新必须单独提交，并更新本文件和证据报告。
3. DeepSeek Harness 与 Codex 只读参考，不加入生产运行依赖。
4. 最终锁定必须包含来源 URL、归档文件 SHA-256、关键入口文件 SHA-256 和验证日期。
5. 任何无法验证来源的代码不得进入 `src/devpilot` 或正式 Skill。
