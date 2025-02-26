import django_filters.rest_framework as filters
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db.models import Exists, OuterRef
from rest_flex_fields import is_expanded
from rest_flex_fields.views import FlexFieldsMixin
from rest_framework import status
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.decorators import action
from rest_framework.metadata import SimpleMetadata
from rest_framework.mixins import (
    CreateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.renderers import BrowsableAPIRenderer
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle, UserRateThrottle
from rest_framework.viewsets import GenericViewSet, ModelViewSet, ReadOnlyModelViewSet
from rest_framework_extensions.mixins import NestedViewSetMixin

from readthedocs.builds.models import Build, Version
from readthedocs.core.utils import trigger_build
from readthedocs.core.utils.extend import SettingsOverrideObject
from readthedocs.notifications.models import Notification
from readthedocs.oauth.models import (
    RemoteOrganization,
    RemoteRepository,
    RemoteRepositoryRelation,
)
from readthedocs.organizations.models import Organization
from readthedocs.projects.models import (
    EnvironmentVariable,
    Project,
    ProjectRelationship,
)
from readthedocs.projects.views.mixins import ProjectImportMixin
from readthedocs.redirects.models import Redirect

from .filters import (
    BuildFilter,
    NotificationFilter,
    ProjectFilter,
    RemoteOrganizationFilter,
    RemoteRepositoryFilter,
    VersionFilter,
)
from .mixins import (
    OrganizationQuerySetMixin,
    ProjectQuerySetMixin,
    RemoteQuerySetMixin,
    UpdateChangeReasonMixin,
    UpdateMixin,
    UserQuerySetMixin,
)
from .permissions import (
    CommonPermissions,
    IsOrganizationAdmin,
    IsOrganizationAdminMember,
    IsProjectAdmin,
)
from .renderers import AlphabeticalSortedJSONRenderer
from .serializers import (
    BuildCreateSerializer,
    BuildSerializer,
    EnvironmentVariableSerializer,
    NotificationSerializer,
    OrganizationSerializer,
    ProjectCreateSerializer,
    ProjectSerializer,
    ProjectUpdateSerializer,
    RedirectCreateSerializer,
    RedirectDetailSerializer,
    RemoteOrganizationSerializer,
    RemoteRepositorySerializer,
    SubprojectCreateSerializer,
    SubprojectDestroySerializer,
    SubprojectSerializer,
    UserSerializer,
    VersionSerializer,
    VersionUpdateSerializer,
)


class APIv3Settings:

    """
    Django REST Framework settings for APIv3.

    Override global DRF settings for APIv3 in particular. All ViewSet should
    inherit from this class to share/apply the same settings all over the APIv3.

    .. note::

        The only settings used from ``settings.REST_FRAMEWORK`` is
        ``DEFAULT_THROTTLE_RATES`` since it's not possible to define here.
    """

    authentication_classes = (TokenAuthentication, SessionAuthentication)
    permission_classes = (CommonPermissions,)

    pagination_class = LimitOffsetPagination
    LimitOffsetPagination.default_limit = 10

    renderer_classes = (AlphabeticalSortedJSONRenderer, BrowsableAPIRenderer)
    throttle_classes = (UserRateThrottle, AnonRateThrottle)
    filter_backends = (filters.DjangoFilterBackend,)
    metadata_class = SimpleMetadata


class ProjectsViewSetBase(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ProjectImportMixin,
    UpdateChangeReasonMixin,
    CreateModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    ReadOnlyModelViewSet,
):
    model = Project
    lookup_field = "slug"
    lookup_url_kwarg = "project_slug"
    filterset_class = ProjectFilter
    queryset = Project.objects.all()
    permit_list_expands = [
        "active_versions",
        "active_versions.last_build",
        "active_versions.last_build.config",
        "organization",
        "teams",
    ]

    def get_view_name(self):
        # Avoid "Base" in BrowseableAPI view's title
        return f"Projects {self.suffix}"

    def get_serializer_class(self):
        """
        Return correct serializer depending on the action.

        For GET it returns a serializer with many fields and on PUT/PATCH/POST,
        it return a serializer to validate just a few fields.
        """
        if self.action in ("list", "retrieve", "superproject"):
            # NOTE: ``superproject`` is the @action defined in the
            # ProjectViewSet that returns the superproject of a project.
            return ProjectSerializer

        if self.action == "create":
            return ProjectCreateSerializer

        if self.action in ("update", "partial_update"):
            return ProjectUpdateSerializer

    def get_queryset(self):
        # Allow hitting ``/api/v3/projects/`` to list their own projects
        if self.basename == "projects" and self.action == "list":
            # We force returning ``Project`` objects here because it's under the
            # ``projects`` view.
            return self.admin_projects(self.request.user)

        # This could be a class attribute and managed on the ``ProjectQuerySetMixin`` in
        # case we want to extend the ``prefetch_related`` to other views as
        # well.
        queryset = super().get_queryset()
        return queryset.prefetch_related(
            "related_projects",
            "domains",
            "tags",
            "users",
        )

    def create(self, request, *args, **kwargs):
        """
        Import Project.

        Override to use a different serializer in the response.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        headers = self.get_success_headers(serializer.data)

        # Use serializer that fully render a Project
        serializer = ProjectSerializer(instance=serializer.instance)

        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )

    def perform_create(self, serializer):
        """
        Import Project.

        Trigger our internal mechanism to import a project after it's saved in
        the database.
        """
        project = super().perform_create(serializer)
        self.finish_import_project(self.request, project)

    @action(detail=True, methods=["get"])
    def superproject(self, request, project_slug):
        """Return the superproject of a ``Project``."""
        project = self.get_object()
        try:
            superproject = project.superprojects.first().parent
            data = self.get_serializer(superproject).data
            return Response(data)
        except Exception:
            return Response(status=status.HTTP_404_NOT_FOUND)


class ProjectsViewSet(SettingsOverrideObject):
    _default_class = ProjectsViewSetBase


class SubprojectRelationshipViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    CreateModelMixin,
    DestroyModelMixin,
    ReadOnlyModelViewSet,
):
    # The main query is done via the ``NestedViewSetMixin`` using the
    # ``parents_query_lookups`` defined when registering the urls.

    model = ProjectRelationship
    lookup_field = "alias"
    lookup_url_kwarg = "alias_slug"
    queryset = ProjectRelationship.objects.all()

    def get_serializer_class(self):
        """
        Return correct serializer depending on the action.

        For GET it returns a serializer with many fields and on POST,
        it return a serializer to validate just a few fields.
        """
        if self.action == "create":
            return SubprojectCreateSerializer

        if self.action == "destroy":
            return SubprojectDestroySerializer

        return SubprojectSerializer

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["parent"] = self._get_parent_project()
        return context

    def create(self, request, *args, **kwargs):
        """Define a Project as subproject of another Project."""
        parent = self._get_parent_project()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(parent=parent)
        headers = self.get_success_headers(serializer.data)

        # Use serializer that fully render a the subproject
        serializer = SubprojectSerializer(instance=serializer.instance)

        return Response(
            serializer.data, status=status.HTTP_201_CREATED, headers=headers
        )


class TranslationRelationshipViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ListModelMixin,
    GenericViewSet,
):
    # The main query is done via the ``NestedViewSetMixin`` using the
    # ``parents_query_lookups`` defined when registering the urls.

    model = Project
    lookup_field = "slug"
    lookup_url_kwarg = "project_slug"
    serializer_class = ProjectSerializer
    queryset = Project.objects.all()


# Inherit order is important here. ``NestedViewSetMixin`` has to be on the left
# of ``ProjectQuerySetMixin`` to make calling ``super().get_queryset()`` work
# properly and filter nested dependencies
class VersionsViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    UpdateMixin,
    UpdateModelMixin,
    ReadOnlyModelViewSet,
):
    model = Version
    lookup_field = "slug"
    lookup_url_kwarg = "version_slug"

    # Allow ``.`` (dots) on version slug
    lookup_value_regex = r"[^/]+"

    filterset_class = VersionFilter
    queryset = Version.internal.all()
    permit_list_expands = [
        "last_build",
        "last_build.config",
    ]

    def get_serializer_class(self):
        """
        Return correct serializer depending on the action.

        For GET it returns a serializer with many fields and on PUT/PATCH/POST,
        it return a serializer to validate just a few fields.
        """
        if self.action in ("list", "retrieve"):
            return VersionSerializer
        return VersionUpdateSerializer

    def update(self, request, *args, **kwargs):
        """Overridden to call ``post_save`` method on the updated version."""
        # Get the current value before updating.
        version = self.get_object()
        was_active = version.active
        result = super().update(request, *args, **kwargs)
        # Get the updated version.
        version = self.get_object()
        version.post_save(was_active=was_active)
        return result


class BuildsViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ReadOnlyModelViewSet,
):
    model = Build
    lookup_field = "pk"
    lookup_url_kwarg = "build_pk"
    serializer_class = BuildSerializer
    filterset_class = BuildFilter
    queryset = Build.internal.all()
    permit_list_expands = [
        "config",
    ]


class BuildsCreateViewSet(BuildsViewSet, CreateModelMixin):
    def get_serializer_class(self):
        if self.action == "create":
            return BuildCreateSerializer

        return super().get_serializer_class()

    def create(self, request, **kwargs):  # pylint: disable=arguments-differ
        project = self._get_parent_project()
        version = self._get_parent_version()

        _, build = trigger_build(project, version=version)

        # TODO: refactor this to be a serializer
        # BuildTriggeredSerializer(build, project, version).data
        data = {
            "build": BuildSerializer(build).data,
            "project": ProjectSerializer(project).data,
            "version": VersionSerializer(build.version).data,
        }

        if build:
            data.update({"triggered": True})
            code = status.HTTP_202_ACCEPTED
        else:
            data.update({"triggered": False})
            code = status.HTTP_400_BAD_REQUEST
        return Response(data=data, status=code)


class NotificationsForUserViewSet(
    APIv3Settings,
    FlexFieldsMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    GenericViewSet,
):

    """
    Endpoint to return all the notifications related to the logged in user.

    Hitting this endpoint while logged in will return notifications attached to:

     - User making the request
     - Organizations where the user is owner/member
     - Projects where the user is admin/member
    """

    model = Notification
    serializer_class = NotificationSerializer
    queryset = Notification.objects.all()

    # Override global permissions here because it doesn't not make sense to hit
    # this endpoint without being logged in. We can't use our
    # ``CommonPermissions`` because it requires the endpoint to be nested under
    # ``projects``
    permission_classes = (IsAuthenticated,)
    filterset_class = NotificationFilter

    def get_queryset(self):
        return Notification.objects.for_user(self.request.user)


class NotificationsProjectViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    model = Notification
    lookup_field = "pk"
    lookup_url_kwarg = "notification_pk"
    serializer_class = NotificationSerializer
    queryset = Notification.objects.all()
    filterset_class = NotificationFilter

    def get_queryset(self):
        project = self._get_parent_project()
        return project.notifications.all()


class NotificationsBuildViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    model = Notification
    lookup_field = "pk"
    lookup_url_kwarg = "notification_pk"
    serializer_class = NotificationSerializer
    queryset = Notification.objects.all()
    filterset_class = NotificationFilter

    def get_queryset(self):
        build = self._get_parent_build()
        return build.notifications.all()


class RedirectsViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    ModelViewSet,
):
    model = Redirect
    lookup_field = "pk"
    lookup_url_kwarg = "redirect_pk"
    queryset = Redirect.objects.all()
    permission_classes = (IsAuthenticated & IsProjectAdmin,)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.select_related("project")

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
            return RedirectCreateSerializer
        return RedirectDetailSerializer

    def perform_create(self, serializer):
        # Inject the project from the URL into the serializer
        serializer.validated_data.update(
            {
                "project": self._get_parent_project(),
            }
        )
        serializer.save()


class EnvironmentVariablesViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    ProjectQuerySetMixin,
    FlexFieldsMixin,
    CreateModelMixin,
    DestroyModelMixin,
    ReadOnlyModelViewSet,
):
    model = EnvironmentVariable
    lookup_field = "pk"
    lookup_url_kwarg = "environmentvariable_pk"
    queryset = EnvironmentVariable.objects.all()
    serializer_class = EnvironmentVariableSerializer
    permission_classes = (IsAuthenticated & IsProjectAdmin,)

    def get_queryset(self):
        queryset = super().get_queryset()
        return queryset.select_related("project")

    def perform_create(self, serializer):
        # Inject the project from the URL into the serializer
        serializer.validated_data.update(
            {
                "project": self._get_parent_project(),
            }
        )
        serializer.save()


class RemoteRepositoryViewSet(
    APIv3Settings, RemoteQuerySetMixin, FlexFieldsMixin, ListModelMixin, GenericViewSet
):
    model = RemoteRepository
    serializer_class = RemoteRepositorySerializer
    filterset_class = RemoteRepositoryFilter
    queryset = RemoteRepository.objects.all()
    permission_classes = (IsAuthenticated,)
    permit_list_expands = ["remote_organization", "projects"]

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .annotate(
                _admin=Exists(
                    RemoteRepositoryRelation.objects.filter(
                        remote_repository=OuterRef("pk"),
                        user=self.request.user,
                        admin=True,
                    )
                )
            )
        )

        if is_expanded(self.request, "remote_organization"):
            queryset = queryset.select_related("organization")

        if is_expanded(self.request, "projects"):
            queryset = queryset.prefetch_related("projects__users")

        return queryset.order_by("organization__name", "full_name").distinct()


class RemoteOrganizationViewSet(
    APIv3Settings, RemoteQuerySetMixin, ListModelMixin, GenericViewSet
):
    model = RemoteOrganization
    serializer_class = RemoteOrganizationSerializer
    filterset_class = RemoteOrganizationFilter
    queryset = RemoteOrganization.objects.all()
    permission_classes = (IsAuthenticated,)


class UsersViewSet(
    APIv3Settings,
    GenericViewSet,
):
    # NOTE: this viewset is only useful for nested URLs required for notifications:
    # /api/v3/users/<username>/notifications/
    # However, accessing to /api/v3/users/ or /api/v3/users/<username>/ will return 404.
    # We can implement these endpoints when we need them, tho.

    model = User
    serializer_class = UserSerializer
    queryset = User.objects.none()
    permission_classes = (IsAuthenticated,)


class NotificationsUserViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    UserQuerySetMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    model = Notification
    lookup_field = "pk"
    lookup_url_kwarg = "notification_pk"
    serializer_class = NotificationSerializer
    queryset = Notification.objects.all()
    filterset_class = NotificationFilter

    def get_queryset(self):
        # Filter the queryset by only notifications attached to the particular user
        # that's making the request to this endpoint
        content_type = ContentType.objects.get_for_model(User)
        return self.queryset.filter(
            attached_to_content_type=content_type,
            attached_to_id=self.request.user.pk,
        )


class OrganizationsViewSetBase(
    APIv3Settings,
    GenericViewSet,
):
    # NOTE: this viewset is only useful for nested URLs required for notifications:
    # /api/v3/organizations/<slug>/notifications/
    # However, accessing to /api/v3/organizations/ or /api/v3/organizations/<slug>/ will return 404.
    # We can implement these endpoints when we need them, tho.
    # Also note that Read the Docs for Business expose this endpoint already.

    model = Organization
    serializer_class = OrganizationSerializer
    queryset = Organization.objects.none()
    permission_classes = (IsAuthenticated,)


class OrganizationsViewSet(SettingsOverrideObject):
    _default_class = OrganizationsViewSetBase


class OrganizationsProjectsViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    OrganizationQuerySetMixin,
    ReadOnlyModelViewSet,
):
    model = Project
    lookup_field = "slug"
    lookup_url_kwarg = "project_slug"
    queryset = Project.objects.all()
    serializer_class = ProjectSerializer
    permission_classes = [IsAuthenticated & IsOrganizationAdminMember]
    permit_list_expands = [
        "organization",
        "organization.teams",
    ]

    def get_view_name(self):
        return f"Organizations Projects {self.suffix}"


class NotificationsOrganizationViewSet(
    APIv3Settings,
    NestedViewSetMixin,
    OrganizationQuerySetMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    model = Notification
    lookup_field = "pk"
    lookup_url_kwarg = "notification_pk"
    serializer_class = NotificationSerializer
    queryset = Notification.objects.all()
    filterset_class = NotificationFilter
    permission_classes = [IsAuthenticated & IsOrganizationAdmin]

    def get_queryset(self):
        organization = self._get_parent_organization()
        return organization.notifications.all()
