"""Значения enum'ов toolbox-service, снятые живьём (см. scripts/probe.py).

Roblox их не документирует, поэтому всё ниже проверено эмпирически:
невалидное значение даёт HTTP 400, валидное — 200.
"""

TOOLBOX_BASE = "https://apis.roblox.com"
SEARCH_PATH = "/toolbox-service/v2/assets:search"
SAVES_PATH = "/toolbox-service/v1/saves"
CATEGORIES_PATH = "/toolbox-service/v1/categories"
THUMBNAILS_URL = "https://thumbnails.roblox.com/v1/assets"
STORE_URL = "https://create.roblox.com/store/asset/{asset_id}"

# Валидные searchCategoryType. Остальные варианты (Models, Image, Mesh, Font,
# Animation, Package, All) отвергаются с 400.
SEARCH_CATEGORIES = ("Model", "Decal", "Audio", "MeshPart", "Plugin", "Video", "FontFamily")

# Человекочитаемые названия для интерфейса бота.
CATEGORY_LABELS = {
    "Model": "🧱 Модели",
    "Decal": "🖼 Картинки",
    "Audio": "🔊 Аудио",
    "MeshPart": "📐 Меши",
    "Plugin": "🔌 Плагины",
    "Video": "🎬 Видео",
    "FontFamily": "🔤 Шрифты",
}

# Валидные sortCategory. Проверено: Relevance, UpdatedTime, Trending, CreateTime.
SORT_CATEGORIES = ("Relevance", "UpdatedTime", "Trending", "CreateTime")
SORT_DIRECTIONS = ("Ascending", "Descending")

# Индекс поиска отдаёт не больше 1000 результатов на запрос и сортирует внутри
# урезанной выборки, поэтому честной ленты "самое новое" не существует.
# Разведка показала, какие связки дают самый свежий контент (минимальный возраст
# ассета на выборке в 300 штук):
#   Trending/Ascending    -> 10 дней, 17 штук моложе месяца
#   Relevance/Descending  -> 17 дней
#   CreateTime/Ascending  -> 52 дня, но на 10 страницах вглубь попадаются 3-дневные
# Поллер крутит эти связки по кругу, чтобы выгребать максимум разнообразия.
HARVEST_SORTS = (
    ("Trending", "Ascending"),
    ("Trending", "Descending"),
    ("Relevance", "Descending"),
    ("CreateTime", "Ascending"),
    ("UpdatedTime", "Ascending"),
)

# categoryPath сужает выборку и вскрывает новый срез индекса — главный источник
# разнообразия. Полное дерево тянется с /toolbox-service/v1/categories (нужен
# API-ключ); этот список — фолбэк на случай, если запрос не прошёл.
FALLBACK_CATEGORY_PATHS = (
    "3d__vehicles",
    "3d__characters",
    "3d__buildings",
    "3d__weapons",
    "3d__nature",
    "models",
    "plugins",
    "audio",
    "2d__decals",
)

# Ротация запросов — ещё один способ достать до незатронутых кусков индекса.
# Односимвольные запросы поиск игнорирует, поэтому только слова.
HARVEST_QUERIES = (
    "",
    "map",
    "car",
    "gun",
    "house",
    "anime",
    "ui",
    "kit",
    "pack",
    "system",
    "npc",
    "sword",
    "tree",
    "furniture",
    "horror",
)

MAX_PAGE_SIZE = 100
# totalResults упирается в 1000, глубже листать бессмысленно.
MAX_PAGES_PER_QUERY = 10
