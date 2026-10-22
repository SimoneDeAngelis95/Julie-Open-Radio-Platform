from django.db import migrations


def backfill(apps, schema_editor):
    Asset = apps.get_model("library", "Asset")
    Broadcast = apps.get_model("library", "Broadcast")
    seen = set()
    for job in Broadcast.objects.filter(asset__category="podcasts", status="completed", ended_at__isnull=False).order_by("ended_at", "id").iterator():
        if job.asset_id not in seen:
            Asset.objects.filter(pk=job.asset_id).update(first_completed_at=job.ended_at, first_broadcast_at=job.started_at or job.scheduled_at)
            seen.add(job.asset_id)


class Migration(migrations.Migration):
    dependencies = [("library", "0006_asset_auto_publish_asset_description_and_more")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
