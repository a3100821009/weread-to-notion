# 微信阅读 → Notion 同步工具

将微信阅读的书架、划线、想法、阅读统计数据同步到 Notion。

## 功能

| 数据类型 | 内容 |
|---------|------|
| 📚 书架 | 所有电子书信息（书名、作者、分类、进度、评分、封面等） |
| ✏️ 划线 | 每本书的全部划线原文，按章节分组 |
| 💭 想法 | 划线想法、章节点评、整本书评 |
| 📊 阅读统计 | 总时长、有效天数、偏好分类/时段/作者、阅读排行 |

## Notion 结构

```
父页面（你指定）
├── 📚 微信阅读书架（数据库）── 每本书一条记录
│       属性：书名、作者、分类、阅读进度、完成状态、评分、划线数、想法数、最近阅读、出版社、ISBN
│       内容：划线 + 想法（按章节分组）
└── 📊 阅读统计（页面）── 总体统计、偏好分析、阅读排行
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

复制 `.env.example` 为 `.env`，填写以下配置：

```bash
cp .env.example .env
```

编辑 `.env`：

```
# 微信阅读 API Key（从微信读书 Agent 获取，格式 wrk-xxx）
WEREAD_API_KEY=wrk-xxxxxxxx

# Notion 集成 Token（从 https://www.notion.so/my-integrations 创建集成）
NOTION_TOKEN=secret_xxxxxxxx

# Notion 父页面 ID（从页面 URL 中提取，32位十六进制）
NOTION_PARENT_PAGE_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

#### 如何获取 Notion 配置

1. **创建 Notion 集成**：访问 https://www.notion.so/my-integrations，新建集成，复制 Token
2. **共享页面给集成**：在 Notion 中打开目标父页面 → 右上角「...」→「Connections」→ 添加你的集成
3. **获取页面 ID**：页面 URL 格式为 `https://www.notion.so/<标题>-<32位ID>`，复制最后 32 位

### 3. 运行同步

```bash
# 完整同步（书架 + 划线 + 统计）
python main.py sync

# 全量同步（忽略增量状态，重新同步所有书）
python main.py sync --full

# 仅同步书架，不同步划线和想法
python main.py sync --no-highlights --no-reviews

# 仅同步书架信息，不同步统计
python main.py sync --no-stats
```

### 其他命令

```bash
# 查看书架列表（不同步到 Notion）
python main.py shelf

# 查看阅读统计（不同步到 Notion）
python main.py stats
python main.py stats --mode monthly   # 本月
python main.py stats --mode annually  # 本年

# 查看某本书的划线
python main.py notes "三体"
python main.py notes "儒林外史"
```

## 增量同步

默认开启增量同步：已同步过且最近阅读时间未变化的书籍会跳过，大幅缩短同步时间。

使用 `--full` 参数可强制全量重新同步所有书籍。

同步状态保存在 `sync_state.json` 文件中。

## 常见问题

**Q: 提示 `WeRead Skill 需要升级`？**  
A: 更新 `weread_notion/weread_client.py` 中的 `SKILL_VERSION` 为最新版本号。

**Q: Notion 同步报 401 错误？**  
A: 检查 `NOTION_TOKEN` 是否正确，以及父页面是否已共享给你的集成。

**Q: 某本书划线同步后内容为空？**  
A: 可能该书没有划线，或划线尚未同步到服务器。
