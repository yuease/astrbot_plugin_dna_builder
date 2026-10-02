"""
模块中文名快照。

官方数据包的 manifest 里只有模块键（abyss.data 之类），没有中文名；这里保存一份从
在线查询取到的对照表，用于本地数据包模式下的模块列表与数据集名解析。
新增模块时缺失的项会回退成模块 id，不影响功能。
"""

MODULE_LABELS: dict[str, str] = {
    "abyss": "深渊",
    "accessory": "饰品与皮肤",
    "achievement": "成就",
    "autochess": "自走棋",
    "backpackpuzzle": "背包解谜",
    "book": "书籍",
    "buff": "BUFF",
    "char": "角色",
    "charext": "角色档案",
    "charext.en": "角色档案（英语）",
    "charext.fr": "角色档案（法语）",
    "charext.jp": "角色档案（日语）",
    "charext.kr": "角色档案（韩语）",
    "charext.tc": "角色档案（繁中）",
    "charvoice": "角色语音",
    "charvoice.en": "角色语音（英语）",
    "charvoice.jp": "角色语音（日语）",
    "charvoice.kr": "角色语音（韩语）",
    "condition": "条件配置",
    "const": "数值常量",
    "convert": "魔之楔转化",
    "cutoff": "限时售卖",
    "draft": "设计稿",
    "dungeon": "副本",
    "dynquest": "动态委托",
    "effect": "特效词条",
    "event": "活动",
    "fish": "钓鱼",
    "forge": "锻造",
    "hardboss": "强敌",
    "headsculpture": "头像雕塑",
    "iconticket": "头像票券",
    "ironsurvival": "铁血生存",
    "jargon": "术语",
    "levelup": "升级消耗",
    "limitedprize": "限定奖池",
    "map": "地图",
    "mod": "魔之楔",
    "monster": "怪物",
    "monstertag": "怪物标签",
    "mount": "坐骑",
    "music": "音乐",
    "npc": "NPC",
    "optreward": "可选奖励",
    "partytopic": "光阴集",
    "partytopic.en": "光阴集（英语）",
    "partytopic.fr": "光阴集（法语）",
    "partytopic.jp": "光阴集（日语）",
    "partytopic.kr": "光阴集（韩语）",
    "partytopic.tc": "光阴集（繁中）",
    "pet": "魔灵",
    "player": "玩家成长",
    "quest": "任务",
    "quest.en": "任务（英语）",
    "quest.fr": "任务（法语）",
    "quest.jp": "任务（日语）",
    "quest.kr": "任务（韩语）",
    "quest.tc": "任务（繁中）",
    "questchain": "任务链",
    "race-lottery": "竞速抽奖",
    "raid": "掠夺副本",
    "region": "区域",
    "reputation": "声望",
    "resource": "资源",
    "reward": "奖励",
    "rouge": "轮回玩法",
    "shop": "商店",
    "skin-colorize": "皮肤染色",
    "skingacha": "皮肤抽奖",
    "solotreasure": "单人寻宝",
    "storysummary": "剧情概要",
    "subregion": "子区域",
    "template": "角色与武器模板",
    "title": "称号",
    "titleframe": "称号框",
    "translations": "多语言翻译表",
    "walnut": "密函",
    "weapon-verify": "武器试炼",
    "weapon": "武器",
}

# 解码与内存开销明显偏大的模块：列表里不主动统计条数，也不进解码缓存。
# （实测其余模块解码都在几十毫秒内，quest 约 30ms / 20MB，只有多语言翻译表 17.7MB 不值得常驻）
HEAVY_MODULES = frozenset({"translations"})


def module_label(module_id: str) -> str:
    """取模块中文名；没有登记时回退成模块 id。"""
    return MODULE_LABELS.get(module_id, module_id)
