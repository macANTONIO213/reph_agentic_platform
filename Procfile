web: gunicorn agentic_platform.wsgi:application
worker: celery -A agentic_platform worker -B --loglevel=info --concurrency=4 --max-tasks-per-child=200
