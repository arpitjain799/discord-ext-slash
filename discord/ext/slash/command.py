from __future__ import annotations
import sys
from typing import (
    Any, Callable, Coroutine, Optional, Mapping,
    Union, Dict, Tuple, TYPE_CHECKING)
from functools import partial, wraps
from inspect import signature, iscoroutinefunction
import discord
from discord.ext import commands
from .logger import logger
from .simples import (
    ApplicationCommandOptionType, ApplicationCommandPermissionType,
    ChoiceEnum)
from .option import Option
from .components import Button, SelectMenu
from .context import BaseContext, Context, ComponentContext
if TYPE_CHECKING:
    # avoid circular import
    from .bot import SlashBot

CheckCoro = Callable[[BaseContext], Coroutine[Any, Any, bool]]
CallbackCoro = Callable[..., Coroutine[None, None, None]]

class BaseCallback(discord.Object):
    """Base class for a callback invoked by an interaction."""
    cog = None
    coro: CallbackCoro

    def __init__(self, coro: CallbackCoro,
                 check: CheckCoro = None):
        self.coro = coro
        async def _default_check(ctx: BaseContext) -> bool:
            return True
        self.check(check or _default_check)

    def __hash__(self) -> int:
        raise NotImplementedError('Callbacks must be hashable to be in a set.')

    def check(self, coro: CheckCoro) -> CheckCoro:
        if iscoroutinefunction(coro):
            self._check = coro
            return coro
        @wraps(coro)
        async def _async_check(ctx: BaseContext) -> bool:
            return coro(ctx)
        self._check = _async_check
        return _async_check

    def invoke(self, ctx: BaseContext) -> None:
        raise NotImplementedError('Callbacks must be invokable.')

class Command(BaseCallback):
    """Represents a slash command.

    The following constructor argument does not map to an attribute:

    :param Callable check:
        A coroutine function to run before calling the command.
        If it returns :const:`False` (not falsy, :const:`False`),
        then the command is not run.

    The following attributes are set by constructor arguments:

    .. attribute:: coro
        :type: Callable[..., Coroutine[None, None, None]]

        (Required) Original callback for the command.
    .. attribute:: id
        :type: Optional[int]

        ID of registered command. Can be None when not yet registered,
        or if not a top-level command.
    .. attribute:: name
        :type: str

        Command name. Defaults to :attr:`coro` ``.__name__``.
    .. attribute:: description
        :type: str

        Description shown in command list. Default :attr:`coro` ``.__doc__``.
    .. attribute:: guild_id
        :type: Optional[int]
        :value: None

        If present, this command only exists in this guild.
    .. attribute:: parent
        :type: Optional[Group]
        :value: None

        Parent (sub)command group.
    .. attribute:: default_permission
        :type: bool
        :value: True

        If :const:`False`, this command is disabled by default
        when the bot is added to a new guild. It must be re-enabled per user
        or role using permissions.

    :raises TypeError:
        if ``coro`` has a required argument (other than ``self``)
        without an annotation.
    :raises ValueError:
        if no ``description`` is specified and ``coro`` has no docstring.
    :raises ValueError:
        if no arguments to ``coro`` are annotated with
        :class:`Context` or a subclass.

    The following attributes are *not* set by constructor arguments:

    .. attribute:: options
        :type: Mapping[str, Option]

        Options for this command. Set by inspecting the function annotations.
    .. attribute:: permissions
        :type: CommandPermissionsDict

        Permission overrides for this command. A dict of guild IDs to dicts of:
        role or user or member objects (partial or real) to boolean
        enable/disable values to grant/deny permissions.
    .. attribute:: default
        :type: bool
        :value: False

        If :const:`True`, invoking the base parent of this command translates
        into invoking this subcommand. (Not settable in arguments.)

    .. decoratormethod:: check

        Set this command's check to this coroutine.
    """
    cog = None
    coro: CallbackCoro
    id: Optional[int]
    name: str
    description: str
    guild_id: Optional[int]
    parent: Optional[Group]
    options: Mapping[str, Option]
    default: bool = False
    default_permission: bool = True
    permissions: CommandPermissionsDict

    def __init__(self, coro: CallbackCoro, **kwargs):
        super().__init__(coro, kwargs.pop('check', None))
        self.id = None
        self.name = kwargs.pop('name', coro.__name__)
        self.description = kwargs.pop('description', coro.__doc__)
        if not self.description:
            raise ValueError(f'Please specify a description for {self.name!r}')
        self.guild_id = kwargs.pop('guild_id', kwargs.pop('guild', None))
        if self.guild_id is not None:
            self.guild_id = int(self.guild_id)
        self.parent = kwargs.pop('parent', None)
        self.default_permission = kwargs.pop('default_permission', True)
        self.permissions = {}
        self._ctx_arg = None
        self.options = {}
        found_self_arg = False
        for param in signature(coro).parameters.values():
            typ = param.annotation
            if isinstance(typ, str):
                try:
                    # evaluate the annotation in its module's context
                    globs = sys.modules[coro.__module__].__dict__
                    typ = eval(typ, globs)
                except:
                    typ = param.empty
            if (
                not (isinstance(typ, Option) or (isinstance(typ, type) and (
                    issubclass(typ, ChoiceEnum) or issubclass(typ, Context))))
                and param.default is param.empty
            ):
                if not found_self_arg:
                    # assume that the first required non-annotated argument
                    # is the self argument to a class' method
                    found_self_arg = True
                    continue
                else:
                    raise TypeError(
                        f'Command {self.name!r} cannot have a '
                        'required argument with no valid annotation')
            try:
                if issubclass(typ, Context):
                    self._ctx_arg = (param.name, typ)
                elif issubclass(typ, ChoiceEnum):
                    typ = Option(description=typ)
            except TypeError: # not even a class
                pass
            if isinstance(typ, Option):
                typ = typ.clone()
                if param.default is param.empty:
                    typ.required = True
                self.options[param.name] = typ
                if typ.name is None:
                    typ.name = param.name
        if self._ctx_arg is None:
            raise ValueError('One argument must be type-hinted slash.Context')

    @property
    def qualname(self) -> str:
        """Fully qualified name of command, including group names."""
        if self.parent is None:
            return self.name
        return self.parent.qualname + ' ' + self.name

    def __str__(self) -> str:
        return self.qualname

    def __hash__(self) -> int:
        return hash((self.name, self.guild_id))

    def _to_dict_common(self, data: dict):
        if self.parent is None:
            data['default_permission'] = self.default_permission

    def to_dict(self) -> dict:
        data = {
            'name': self.name,
            'description': self.description
        }
        if self.options:
            data['options'] = [opt.to_dict() for opt in self.options.values()]
        self._to_dict_common(data)
        return data

    def perms_dict(self, guild_id: Optional[int]) -> dict:
        perms = []
        final = self.permissions.get(None, {}).copy()
        final.update(self.permissions.get(guild_id, {}).items())
        for (oid, type), perm in final.items():
            perms.append({
                'id': oid,
                'type': type.value,
                'permission': perm
            })
        return {'id': self.id, 'permissions': perms}

    def add_perm(
        self, target: Union[discord.Role, discord.abc.User, discord.Object],
        perm: bool, guild_id: Optional[int] = ...,
        type: ApplicationCommandPermissionType = None
    ):
        """Add a permission override.

        :param target: The role or user to assign this permission to.
        :type target: Union[discord.Role, PartialRole, discord.Member,
            discord.User, PartialMember, discord.Object]
        :param bool perm:
            :const:`True` to grant permission, :const:`False` to deny it
        :param guild_id:
            The guild ID to set the permission for, or :const:`None` to apply
            this to the defaults that all guilds inherit for this command.
            If specified, overrides ``target.guild.id``.
            Must be specified if ``target`` is a :class:`~discord.Object` or
            a guildless :class:`~discord.User`.
        :type guild_id: Optional[int]
        :param ApplicationCommandPermissionType type:
            The type of permission grant this is,
            :attr:`~ApplicationCommandPermissionType.ROLE` or
            :attr:`~ApplicationCommandPermissionType.USER`.
            Must be specified if ``target`` is a :class:`~discord.Object`.

        Generally there are four ways of calling this:

        * ``add_perm(target, perm)`` will infer ``guild_id`` and ``type``
          from ``target.guild.id`` and the type of ``target`` (respectively).
        * ``add_perm(target, perm, guild_id)`` will infer the type, but
          manually set the guild ID (e.g. with a :class:`~discord.User` and
          not a :class:`~discord.Member`).
        * ``add_perm(discord.Object(id), perm, guild_id, type)`` will manually
          set the guild ID and type since all you have is an ID.
        * ``add_perm(..., guild_id=None)`` will do any of the above but apply
          the permissions to the defaults that all specific-guild permissions
          will inherit from, instead of applying to any particular guild.

        :raises ValueError: if ``type`` is unspecified but cannot be inferred.
        :raises ValueError:
            if ``guild_id`` is unspecified but cannot be inferred.
        """
        if type is None:
            if isinstance(target, discord.Role):
                type = ApplicationCommandPermissionType.ROLE
            elif isinstance(target, discord.abc.User):
                type = ApplicationCommandPermissionType.USER
            else:
                raise ValueError(
                    'Must specify type if target is not a discord.py model')
        if guild_id is ...:
            if isinstance(target, (discord.Role, discord.Member)):
                guild_id = target.guild.id
            else:
                raise ValueError(
                    'Must specify guild_id if target is not a guilded object')
        self.permissions.setdefault(guild_id, {})[target.id, type] = perm

    async def invoke(self, ctx: Context) -> None:
        if not await self.can_run(ctx):
            raise commands.CheckFailure(
                f'The check functions for {self.qualname} failed.')
        logger.debug('User %s running, in guild %s channel %s, command: %s',
                     ctx.author and ctx.author.id,
                     ctx.guild and ctx.guild.id,
                     ctx.channel.id, ctx.command.qualname)
        await self.invoke_parents(ctx)
        if self.cog is not None:
            await self.coro(self.cog, **ctx.options)
        else:
            await self.coro(**ctx.options)

    async def can_run(self, ctx: Context) -> bool:
        parents = []  # highest level parent last
        cogs = []
        parent = self.parent
        while parent is not None:
            if parent.cog is not None:
                if hasattr(parent.cog, 'cog_check'):
                    if parent.cog.cog_check not in cogs:
                        cogs.append(parent.cog.cog_check)
                parents.append(partial(parent._check, parent.cog))
            else:
                parents.append(parent._check)
            parent = parent.parent
        parents.extend(cogs)
        parents.extend(ctx.client._checks)
        parents.reverse()  # highest level parent first
        parents.append(self._check)
        for check in parents:
            if await check(ctx) is False:
                return False
        return True

    async def invoke_parents(self, ctx: Context):
        parents = []
        parent = self.parent
        while parent is not None:
            if parent.cog is not None:
                parents.append(partial(parent.coro, parent.cog))
            else:
                parents.append(parent.coro)
            parent = parent.parent
        parents.reverse()
        for coro in parents:
            await coro(ctx)

class Group(Command):
    """Represents a group of slash commands.
    Attributes and constructor arguments are the same as :class:`Command`
    unless documented below.

    :param Callable coro:
        (Required) Coroutine function invoked when a subcommand is called.
        (This is not a check! Register a check using :meth:`~Command.check`.)

    .. attribute:: slash
        :type: Mapping[str, Union[Group, Command]]

        Subcommands of this group.

    .. decoratormethod:: slash_cmd(**kwargs)

        See :meth:`SlashBot.slash_cmd`.
    .. decoratormethod:: slash_group(**kwargs)

        See :meth:`SlashBot.slash_group`.
    """
    cog = None
    slash: Mapping[str, Union[Group, Command]]

    def __init__(self, coro: CallbackCoro, **kwargs):
        super().__init__(coro, **kwargs)
        self.slash = {}

    def slash_cmd(self, **kwargs):
        kwargs['parent'] = self
        def decorator(func):
            cmd = Command(func, **kwargs)
            cmd.cog = self.cog
            self.slash[cmd.name] = cmd
            return cmd
        return decorator

    def add_slash(self, func, **kwargs):
        """See :meth:`SlashBot.add_slash`."""
        self.slash_cmd(**kwargs)(func)

    def slash_group(self, **kwargs):
        kwargs['parent'] = self
        def decorator(func):
            group = Group(func, **kwargs)
            group.cog = self.cog
            self.slash[group.name] = group
            return group
        return decorator

    def add_slash_group(self, func, **kwargs):
        """See :meth:`SlashBot.add_slash_group`."""
        self.slash_group(**kwargs)(func)

    def to_dict(self):
        data = {
            'name': self.name,
            'description': self.description
        }
        if self.slash:
            data['options'] = []
            for sub in self.slash.values():
                ddict = sub.to_dict()
                if isinstance(sub, Group):
                    ddict['type'] = ApplicationCommandOptionType.SUB_COMMAND_GROUP
                elif isinstance(sub, Command):
                    ddict['type'] = ApplicationCommandOptionType.SUB_COMMAND
                else:
                    raise ValueError(f'What is a {type(sub).__name__} doing here?')
                data['options'].append(ddict)
        self._to_dict_common(data)
        return data

class ComponentCallback(BaseCallback):
    """A callback for a message component interaction.

    The following constructor argument does not map directly to an attribute:

    :param matcher:
        Function called to decide whether to use this callback;
        or coroutine function called for the same reason;
        or a :class:`Button` or :class:`SelectMenu` to bind to;
        or a string ID to bind to.
    :type matcher: Union[Callable[[Context], bool],
                         Callable[[Context], Coroutine[None, None, bool]],
                         Button, SelectMenu, str]

    The following attributes are set by constructor arguments:

    .. attribute:: coro
        :type: Callable[[Context], Coroutine[None, None, None]]

        (Required) Original callback for the component.

    .. attribute:: max_uses
        :type: Optional[int]

        If set, the callback can only be called this many times
        before being deregistered.

    The following attributes are *not* directly set by constructor arguments:

    .. attribute:: matcher
        :type: Callable[[Context], Coroutine[None, None, bool]]

        Harmonized callback-use decider.
    """
    id = 0 # to fulfil Object requirements
    cog = None
    max_uses: Optional[int]
    matcher: CheckCoro

    def __init__(self, coro: CallbackCoro, matcher: Union[
        Callable[[ComponentContext], bool], CheckCoro, Button, SelectMenu, str
    ], **kwargs):
        super().__init__(coro, check=kwargs.pop('check', None))
        if iscoroutinefunction(matcher):
            self.matcher = matcher
        elif isinstance(matcher, (Button, SelectMenu)):
            async def match_comp(ctx: ComponentContext) -> bool:
                return ctx.custom_id == matcher.custom_id
            self.matcher = match_comp
        elif isinstance(matcher, str):
            async def match_id(ctx: ComponentContext) -> bool:
                return ctx.custom_id == matcher
            self.matcher = match_id
        else:
            async def match(ctx: ComponentContext) -> bool:
                return matcher(ctx)
            self.matcher = match
        self.max_uses = kwargs.pop('max_uses', None)

    def __hash__(self) -> int:
        return hash((self.coro, self.matcher))

    async def invoke(self, ctx: ComponentContext) -> None:
        if await self._check(ctx) is False:
            raise commands.CheckFailure(
                f'The check function for {self!r} failed.')
        logger.debug('Interaction %s for %r by user %s in guild %s channel %s',
                     ctx.id, ctx.custom_id, ctx.author and ctx.author.id,
                     ctx.guild and ctx.guild.id,
                     ctx.channel and ctx.channel.id)
        if self.max_uses is not None:
            self.max_uses -= 1
            if self.max_uses <= 0:
                self.deregister(ctx.bot)
        if self.cog is not None:
            await self.coro(self.cog, ctx, *ctx.values)
        else:
            await self.coro(ctx, *ctx.values)

    def deregister(self, bot: SlashBot) -> None:
        """Deregister this callback (and probably garbage collect it soon)."""
        bot.comp_callbacks.discard(self)
        bot.dispatch('component_callback_deregister', self)

def cmd(**kwargs):
    """Decorator transforming a function into a :class:`Command`."""
    def decorator(func):
        return Command(func, **kwargs)
    return decorator

def group(**kwargs):
    """Decorator transforming a function into a :class:`Group`."""
    def decorator(func):
        return Group(func, **kwargs)
    return decorator

def callback(matcher, **kwargs):
    """Decorator transforming function into a :class:`ComponentCallback`."""
    def decorator(func):
        return ComponentCallback(func, matcher, **kwargs)
    return decorator

CommandPermissionsDict = Dict[Optional[int], Dict[Tuple[
    int, ApplicationCommandPermissionType], bool]]

def permit(
    target: Union[discord.Role, discord.abc.User, discord.Object],
    perm: bool, guild_id: Optional[int] = ...,
    type: ApplicationCommandPermissionType = None
):
    """Decorator **on top of a command** that adds a permissions overwrite."""
    def decorator(func: Command):
        func.add_perm(target, perm, guild_id, type)
        return func
    return decorator
