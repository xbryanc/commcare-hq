# -*- coding: utf-8 -*-
# Generated by Django 1.10.7 on 2017-07-23 20:08
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('rch', '0003_rchrecord_details'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='rchrecord',
            name='details_doc_id',
        ),
    ]
