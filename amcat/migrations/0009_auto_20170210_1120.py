# -*- coding: utf-8 -*-
# Generated by Django 1.9.12 on 2017-02-10 11:20
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('amcat', '0008_merge'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='plugin',
            name='plugin_type',
        ),
        migrations.DeleteModel(
            name='Plugin',
        ),
        migrations.DeleteModel(
            name='PluginType',
        ),
    ]
