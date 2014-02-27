from django.db import models
from django.db.models import Q
from django.core.exceptions import ValidationError
from django.dispatch import receiver
from django.db.models.signals import post_save
from django.db.models.loading import get_model
from django.core.mail import send_mail

from ..utils.models import BaseModel
from ..utils.fields import ReservedKeywordsAutoSlugField
from ..utils.decorators import autoconnect
from .constants import BOARD_RESERVED_KEYWORDS


@autoconnect
class Board(BaseModel):
    name = models.CharField(max_length=255)
    slug = ReservedKeywordsAutoSlugField(
        populate_from='name', unique_with='account', editable=True,
        reserved_keywords=BOARD_RESERVED_KEYWORDS)

    account = models.ForeignKey('accounts.Account')
    created_by = models.ForeignKey('users.User')

    is_shared = models.BooleanField(default=False)

    thumbnail_sm_path = models.TextField(blank=True)
    thumbnail_md_path = models.TextField(blank=True)
    thumbnail_lg_path = models.TextField(blank=True)

    class Meta:
        announce = True

    def __str__(self):
        return self.name

    @property
    def announce_room(self):
        return 'a{}'.format(self.account_id)

    @property
    def serializer(self):
        from .serializers import BoardSerializer
        return BoardSerializer(self)

    def get_thumbnail_url(self, size='sm'):
        """
        Returns full thumbnail url.
        BUCKET_URL + thumbnail path
        """
        pass

    def is_user_collaborator(self, user, permission=None):
        """
        Returns `True` if a user is a collaborator on this
        Board, `False` otherwise. Optionally checks if a user
        is a collaborator with a specific permission.
        """
        collaborators = BoardCollaborator.objects.filter(board=self, user=user)
        read_permission = BoardCollaborator.READ_PERMISSION
        write_permission = BoardCollaborator.WRITE_PERMISSION

        if permission == read_permission:
            collaborators = collaborators.filter(
                Q(permission=write_permission) | Q(permission=read_permission))
        elif permission == write_permission:
            collaborators = collaborators.filter(permission=write_permission)

        return collaborators.exists()


@receiver([post_save], sender=Board)
def create_owner_collaborator(instance, created=False, **kwargs):
    """
    Create BoardCollaborator for account owner after creating a Board.
    """
    if created:
        account_owner = instance.account.owner

        BoardCollaborator.objects.create(
            board=instance,
            user=account_owner.user,
            permission=BoardCollaborator.WRITE_PERMISSION
        )


class BoardCollaborator(BaseModel):
    READ_PERMISSION = 'read'
    WRITE_PERMISSION = 'write'

    PERMISSION_CHOICES = (
        (READ_PERMISSION, 'Read'),
        (WRITE_PERMISSION, 'Read and Write'),
    )

    board = models.ForeignKey('boards.Board')
    user = models.ForeignKey('users.User', blank=True, null=True)
    invited_user = models.ForeignKey('invitations.InvitedUser',
                                     blank=True, null=True)

    permission = models.CharField(max_length=5, choices=PERMISSION_CHOICES)

    class Meta:
        unique_together = (
            ('board', 'user'),
            ('board', 'invited_user'),
        )

    def __str__(self):
        return str(self.user) if self.user else str(self.invited_user)

    def save(self, force_insert=False, force_update=False, **kwargs):
        """
        Performs all steps involved in validating  whenever
        a model object is saved.
        """
        self.full_clean()

        return super(BoardCollaborator, self).save(
            force_insert, force_update, **kwargs)

    def clean(self):
        """
        Validates that either a user or an invited_user is set.
        """
        if self.user and self.invited_user:
            raise ValidationError(
                'Both user and invited_user cannot be set together.')
        elif not self.user and not self.invited_user:
            raise ValidationError('Either user or invited_user must be set.')


class BoardCollaboratorRequest(BaseModel):
    first_name = models.CharField(max_length=30, blank=True)
    last_name = models.CharField(max_length=30, blank=True)
    email = models.EmailField(blank=True)
    user = models.ForeignKey('users.User', blank=True, null=True)
    board = models.ForeignKey('boards.Board')
    message = models.TextField(blank=True)

    class Meta:
        unique_together = (
            ('email', 'board'),
            ('user', 'board'),
        )

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        """
        When saving a BoardCollaboratorRequest, try to set first_name,
        last_name, and email if a user is given. If no user is given,
        try to find an existing user with matching email.
        """
        User = get_model('users', 'User')

        if not self.user:
            try:
                self.user = User.objects.get(email=self.email)
            except User.DoesNotExist:
                pass

        if self.user:
            self.first_name = self.user.first_name
            self.last_name = self.user.last_name
            self.email = self.user.email

        self.full_clean()

        return super(BoardCollaboratorRequest, self).save(*args, **kwargs)

    def clean(self):
        """
        Validates that either a user or an email is set.
        """
        if not self.user and not self.email:
            raise ValidationError('Either user or email must be set.')

    def accept(self):
        """
        - Creates an InvitedUser for this board
        - Creates a BoardCollaborator for invited_user
        - Sets it in the InvitedUser.board_collaborators
        - Sends invitation
        - Deletes BoardCollaboratorRequest
        """
        InvitedUser = get_model('invitations', 'InvitedUser')

        invited_user_data = {
            'first_name': self.first_name,
            'last_name': self.last_name,
            'email': self.email,
            'user': self.user,
            'account': self.board.account,
            'created_by': self.board.account.owner.user,
        }

        invited_user = InvitedUser.objects.create(**invited_user_data)

        board_collaborator = BoardCollaborator.objects.create(
            board=self.board,
            invited_user=invited_user,
            permission=BoardCollaborator.READ_PERMISSION
        )

        invited_user.board_collaborators.add(board_collaborator)

        invited_user.send_invite()

        self.delete()

    def reject(self):
        """
        Deletes BoardCollaboratorRequest.
        """
        self.delete()

    def notify_account_owner(self):
        message = '{} wants to join your board'.format(self.email)

        return send_mail(
            'Blimp Board Collaborator Request',
            message,
            'from@example.com',
            [self.email],
            fail_silently=False
        )
