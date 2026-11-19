import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import catalog, config, models

wanted = sys.argv[1:] or [config.DEFAULT_MODEL]

for model_id in wanted:
    entry = catalog.get(model_id)
    if entry is None:
        sys.exit(f"Неизвестная модель {model_id}. Доступны: {', '.join(catalog.ids())}")
    if models.installed(model_id):
        print(f"Модель {model_id} уже на месте: {models.path(model_id)}")
        continue
    print(f"Скачиваю {model_id} ({entry['repo']}), около {entry['size_mb']} МБ")
    models.fetch(model_id)
    print(f"Готово: {models.path(model_id)}")
