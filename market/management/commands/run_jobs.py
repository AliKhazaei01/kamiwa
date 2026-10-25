from django.core.management.base import BaseCommand
from market.jobs import run_jobs


class Command(BaseCommand):
    help = 'Run bounded test-delivery, admin rotation and secret-retention jobs.'

    def handle(self, *args, **options):
        self.stdout.write(str(run_jobs()))
