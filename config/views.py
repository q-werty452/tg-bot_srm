"""Заглушка разделов, которые появятся на следующих шагах."""

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


def make_stub(page_title: str, section: str):
    @login_required
    def view(request):
        return render(request, "stub.html",
                      {"page_title": page_title, "section": section})
    return view
