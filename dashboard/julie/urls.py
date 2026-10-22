from django.contrib.auth import views as auth_views
from django.urls import include, path
from library.views import health
from library.onboarding import setup

urlpatterns = [
    path("setup/", setup, name="setup"),
    path("health/", health, name="health"),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("library.urls")),
]
