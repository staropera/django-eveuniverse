from collections import namedtuple
import logging
import math

import networkx as nx
from networkx.exception import NetworkXNoPath, NodeNotFound

from django.core.cache import cache
from django.db import models, transaction

from allianceauth.eveonline.evelinks import eveimageserver, dotlan, zkillboard
from allianceauth.eveonline.models import (
    EveAllianceInfo,
    EveCorporationInfo,
    EveCharacter,
)

from . import __title__
from .app_settings import (
    EVEUNIVERSE_LOAD_DOGMAS,
    EVEUNIVERSE_LOAD_MARKET_GROUPS,
    EVEUNIVERSE_LOAD_ASTEROID_BELTS,
    EVEUNIVERSE_LOAD_GRAPHICS,
    EVEUNIVERSE_LOAD_MOONS,
    EVEUNIVERSE_LOAD_PLANETS,
    EVEUNIVERSE_LOAD_STARGATES,
    EVEUNIVERSE_LOAD_STARS,
    EVEUNIVERSE_LOAD_STATIONS,
)

from .managers import (
    EveUniverseBaseModelManager,
    EveUniverseEntityModelManager,
    EvePlanetChildrenManager,
    EvePlanetManager,
    EveStargateManager,
    EveStationManager,
    EveEntityManager,
)
from .utils import LoggerAddTag


logger = LoggerAddTag(logging.getLogger(__name__), __title__)

NAMES_MAX_LENGTH = 100
ROUTE_CACHE_DURATION = 86_400


EsiMapping = namedtuple(
    "EsiMapping",
    [
        "esi_name",
        "is_optional",
        "is_pk",
        "is_fk",
        "related_model",
        "is_parent_fk",
        "is_charfield",
        "create_related",
    ],
)


class EveUniverseBaseModel(models.Model):
    """Base properties and features"""

    objects = EveUniverseBaseModelManager()

    class Meta:
        abstract = True

    @classmethod
    def esi_mapping(cls) -> dict:
        field_mappings = cls._eve_universe_meta_attr("field_mappings")
        functional_pk = cls._eve_universe_meta_attr("functional_pk")
        parent_fk = cls._eve_universe_meta_attr("parent_fk")
        dont_create_related = cls._eve_universe_meta_attr("dont_create_related")
        mapping = dict()
        for field in [
            field
            for field in cls._meta.get_fields()
            if not field.auto_created
            and field.name != "last_updated"
            and field.name not in cls._disabled_fields()
            and not field.many_to_many
        ]:
            if field_mappings and field.name in field_mappings:
                esi_name = field_mappings[field.name]
            else:
                esi_name = field.name

            if field.primary_key is True:
                is_pk = True
                esi_name = cls.esi_pk()
            elif functional_pk and field.name in functional_pk:
                is_pk = True
            else:
                is_pk = False

            if parent_fk and is_pk and field.name in parent_fk:
                is_parent_fk = True
            else:
                is_parent_fk = False

            if isinstance(field, models.ForeignKey):
                is_fk = True
                related_model = field.related_model
            else:
                is_fk = False
                related_model = None

            if dont_create_related and field.name in dont_create_related:
                create_related = False
            else:
                create_related = True

            mapping[field.name] = EsiMapping(
                esi_name=esi_name,
                is_optional=field.has_default(),
                is_pk=is_pk,
                is_fk=is_fk,
                related_model=related_model,
                is_parent_fk=is_parent_fk,
                is_charfield=isinstance(field, (models.CharField, models.TextField)),
                create_related=create_related,
            )

        return mapping

    @classmethod
    def _disabled_fields(cls) -> set:
        """returns name of fields that must not be loaded from ESI"""
        return {}

    @classmethod
    def _eve_universe_meta_attr(cls, attr_name: str, is_mandatory: bool = False):
        """returns value of an attribute from EveUniverseMeta or None"""
        if not hasattr(cls, "EveUniverseMeta"):
            raise ValueError("EveUniverseMeta not defined for class %s" % cls.__name__)

        if hasattr(cls.EveUniverseMeta, attr_name):
            value = getattr(cls.EveUniverseMeta, attr_name)
        else:
            value = None
            if is_mandatory:
                raise ValueError(
                    "Mandatory attribute EveUniverseMeta.%s not defined "
                    "for class %s" % (attr_name, cls.__name__)
                )
        return value


class EveUniverseEntityModel(EveUniverseBaseModel):
    """Eve Universe Entity model
    
    Entity models are normal Eve entities that have a dedicated ESI endpoint
    """

    DEFAULT_ICON_SIZE = 64

    id = models.PositiveIntegerField(primary_key=True, help_text="Eve Online ID")
    name = models.CharField(
        max_length=NAMES_MAX_LENGTH, default="", help_text="Eve Online name"
    )
    last_updated = models.DateTimeField(
        auto_now=True,
        help_text="When this object was last updated from ESI",
        db_index=True,
    )

    objects = EveUniverseEntityModelManager()

    class Meta:
        abstract = True

    def __repr__(self):
        return "{}(id={}, name='{}')".format(
            self.__class__.__name__, self.id, self.name
        )

    def __str__(self):
        return self.name

    @classmethod
    def esi_pk(cls) -> str:
        """returns the name of the pk column on ESI that must exist"""
        return cls._eve_universe_meta_attr("esi_pk", is_mandatory=True)

    @classmethod
    def has_esi_path_list(cls) -> str:
        return bool(cls._eve_universe_meta_attr("esi_path_list"))

    @classmethod
    def esi_path_list(cls) -> str:
        return cls._esi_path("list")

    @classmethod
    def esi_path_object(cls) -> str:
        return cls._esi_path("object")

    @classmethod
    def _esi_path(cls, variant: str) -> tuple:
        attr_name = f"esi_path_{str(variant)}"
        path = cls._eve_universe_meta_attr(attr_name, is_mandatory=True)
        if len(path.split(".")) != 2:
            raise ValueError(f"{attr_name} not valid")
        return path.split(".")

    @classmethod
    def children(cls) -> dict:
        """returns the mapping of children for this class"""
        mappings = cls._eve_universe_meta_attr("children")
        return mappings if mappings else dict()

    @classmethod
    def inline_objects(cls) -> dict:
        """returns a dict of inline objects if any"""
        inline_objects = cls._eve_universe_meta_attr("inline_objects")
        return inline_objects if inline_objects else dict()

    @classmethod
    def is_list_only_endpoint(cls) -> bool:
        esi_path_list = cls._eve_universe_meta_attr("esi_path_list")
        esi_path_object = cls._eve_universe_meta_attr("esi_path_object")
        return esi_path_list and esi_path_object and esi_path_list == esi_path_object


class EveUniverseInlineModel(EveUniverseBaseModel):
    """Eve Universe Inline model
    
    Inline models are objects which do not have a dedicated ESI endpoint and are 
    provided through the endpoint of another entity

    This class is also used for static Eve data
    """

    class Meta:
        abstract = True


class EveAncestry(EveUniverseEntityModel):
    """"Ancestry in Eve Online"""

    eve_bloodline = models.ForeignKey(
        "EveBloodline", on_delete=models.CASCADE, related_name="eve_bloodlines"
    )
    description = models.TextField()
    icon_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    short_description = models.TextField(default="")

    class EveUniverseMeta:
        esi_pk = "id"
        esi_path_list = "Universe.get_universe_ancestries"
        esi_path_object = "Universe.get_universe_ancestries"
        field_mappings = {"eve_bloodline": "bloodline_id"}


class EveAsteroidBelt(EveUniverseEntityModel):
    """"Asteroid belt in Eve Online"""

    eve_planet = models.ForeignKey(
        "EvePlanet", on_delete=models.CASCADE, related_name="eve_asteroid_belts"
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )

    objects = EvePlanetChildrenManager("asteroid_belts")

    class EveUniverseMeta:
        esi_pk = "asteroid_belt_id"
        esi_path_object = "Universe.get_universe_asteroid_belts_asteroid_belt_id"
        field_mappings = {
            "eve_planet": "planet_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }


class EveBloodline(EveUniverseEntityModel):
    """"Bloodline in Eve Online"""

    eve_race = models.ForeignKey(
        "EveRace",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_bloodlines",
    )
    eve_ship_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="eve_bloodlines"
    )
    charisma = models.PositiveIntegerField()
    corporation_id = models.PositiveIntegerField()
    description = models.TextField()
    intelligence = models.PositiveIntegerField()
    memory = models.PositiveIntegerField()
    perception = models.PositiveIntegerField()
    willpower = models.PositiveIntegerField()

    class EveUniverseMeta:
        esi_pk = "bloodline_id"
        esi_path_list = "Universe.get_universe_bloodlines"
        esi_path_object = "Universe.get_universe_bloodlines"
        field_mappings = {"eve_race": "race_id", "eve_ship_type": "ship_type_id"}


class EveCategory(EveUniverseEntityModel):
    """category in Eve Online"""

    published = models.BooleanField()

    class EveUniverseMeta:
        esi_pk = "category_id"
        esi_path_list = "Universe.get_universe_categories"
        esi_path_object = "Universe.get_universe_categories_category_id"
        children = {"groups": "EveGroup"}


class EveConstellation(EveUniverseEntityModel):
    """constellation in Eve Online"""

    eve_region = models.ForeignKey(
        "EveRegion", on_delete=models.CASCADE, related_name="eve_constellations"
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )

    class EveUniverseMeta:
        esi_pk = "constellation_id"
        esi_path_list = "Universe.get_universe_constellations"
        esi_path_object = "Universe.get_universe_constellations_constellation_id"
        field_mappings = {
            "eve_region": "region_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }
        children = {"systems": "EveSolarSystem"}


class EveDogmaAttribute(EveUniverseEntityModel):
    """"Dogma Attribute in Eve Online"""

    eve_unit = models.ForeignKey(
        "EveUnit",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_units",
    )
    default_value = models.FloatField(default=None, null=True)
    description = models.TextField(default="")
    display_name = models.CharField(max_length=NAMES_MAX_LENGTH, default="")
    high_is_good = models.BooleanField(default=None, null=True)
    icon_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    published = models.BooleanField(default=None, null=True)
    stackable = models.BooleanField(default=None, null=True)

    class EveUniverseMeta:
        esi_pk = "attribute_id"
        esi_path_list = "Dogma.get_dogma_attributes"
        esi_path_object = "Dogma.get_dogma_attributes_attribute_id"
        field_mappings = {"eve_unit": "unit_id"}


class EveDogmaEffect(EveUniverseEntityModel):
    """"Dogma effect in Eve Online"""

    description = models.TextField(default="")
    disallow_auto_repeat = models.BooleanField(default=None, null=True)
    discharge_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="discharge_attribute_effects",
    )
    display_name = models.CharField(max_length=NAMES_MAX_LENGTH, default="")
    duration_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="duration_attribute_effects",
    )
    effect_category = models.PositiveIntegerField(default=None, null=True)
    electronic_chance = models.BooleanField(default=None, null=True)
    falloff_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="falloff_attribute_effects",
    )
    icon_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    is_assistance = models.BooleanField(default=None, null=True)
    is_offensive = models.BooleanField(default=None, null=True)
    is_warp_safe = models.BooleanField(default=None, null=True)
    post_expression = models.PositiveIntegerField(default=None, null=True)
    pre_expression = models.PositiveIntegerField(default=None, null=True)
    published = models.BooleanField(default=None, null=True)
    range_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="range_attribute_effects",
    )
    range_chance = models.BooleanField(default=None, null=True)
    tracking_speed_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="tracking_speed_attribute_effects",
    )

    class EveUniverseMeta:
        esi_pk = "effect_id"
        esi_path_list = "Dogma.get_dogma_effects"
        esi_path_object = "Dogma.get_dogma_effects_effect_id"
        field_mappings = {
            "discharge_attribute": "discharge_attribute_id",
            "duration_attribute": "duration_attribute_id",
            "falloff_attribute": "falloff_attribute_id",
            "range_attribute": "range_attribute_id",
            "tracking_speed_attribute": "tracking_speed_attribute_id",
        }
        inline_objects = {
            "modifiers": "EveDogmaEffectModifier",
        }


class EveDogmaEffectModifier(EveUniverseInlineModel):
    """Modifier for a dogma effect in Eve Online"""

    domain = models.CharField(max_length=NAMES_MAX_LENGTH, default="")
    eve_dogma_effect = models.ForeignKey(
        "EveDogmaEffect", on_delete=models.CASCADE, related_name="modifiers"
    )
    func = models.CharField(max_length=NAMES_MAX_LENGTH)
    modified_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="modified_attribute_modifiers",
    )
    modifying_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="modifying_attribute_modifiers",
    )
    modifying_effect = models.ForeignKey(
        "EveDogmaEffect",
        on_delete=models.SET_DEFAULT,
        null=True,
        default=None,
        blank=True,
        related_name="modifying_effect_modifiers",
    )
    operator = models.PositiveIntegerField(default=None, null=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["eve_dogma_effect", "func"], name="functional PK"
            )
        ]

    class EveUniverseMeta:
        parent_fk = "eve_dogma_effect"
        functional_pk = [
            "eve_dogma_effect",
            "func",
        ]
        field_mappings = {
            "modified_attribute": "modified_attribute_id",
            "modifying_attribute": "modifying_attribute_id",
            "modifying_effect": "effect_id",
        }

    def __repr__(self) -> str:
        return (
            f"EveEffectModifier(eve_type='{self.eve_type}', "
            f"effect_id={self.effect_id})"
        )


class EveFaction(EveUniverseEntityModel):
    """"faction in Eve Online"""

    corporation_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    description = models.TextField()
    eve_solar_system = models.ForeignKey(
        "EveSolarSystem",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_factions",
    )
    is_unique = models.BooleanField()
    militia_corporation_id = models.PositiveIntegerField(
        default=None, null=True, db_index=True
    )
    size_factor = models.FloatField()
    station_count = models.PositiveIntegerField()
    station_system_count = models.PositiveIntegerField()

    class EveUniverseMeta:
        esi_pk = "faction_id"
        esi_path_list = "Universe.get_universe_factions"
        esi_path_object = "Universe.get_universe_factions"
        field_mappings = {"eve_solar_system": "solar_system_id"}


class EveGraphic(EveUniverseEntityModel):
    """graphic in Eve Online"""

    FILENAME_MAX_CHARS = 255

    collision_file = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    graphic_file = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    icon_folder = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    sof_dna = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    sof_fation_name = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    sof_hull_name = models.CharField(max_length=FILENAME_MAX_CHARS, default="")
    sof_race_name = models.CharField(max_length=FILENAME_MAX_CHARS, default="")

    class EveUniverseMeta:
        esi_pk = "graphic_id"
        esi_path_list = "Universe.get_universe_graphics"
        esi_path_object = "Universe.get_universe_graphics_graphic_id"


class EveGroup(EveUniverseEntityModel):
    """group in Eve Online"""

    eve_category = models.ForeignKey(
        "EveCategory", on_delete=models.CASCADE, related_name="eve_groups"
    )
    published = models.BooleanField()

    class EveUniverseMeta:
        esi_pk = "group_id"
        esi_path_list = "Universe.get_universe_groups"
        esi_path_object = "Universe.get_universe_groups_group_id"
        field_mappings = {"eve_category": "category_id"}
        children = {"types": "EveType"}


class EveMarketGroup(EveUniverseEntityModel):
    """"Market Group in Eve Online"""

    description = models.TextField()
    parent_market_group = models.ForeignKey(
        "self",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="market_group_children",
    )

    class EveUniverseMeta:
        esi_pk = "market_group_id"
        esi_path_list = "Market.get_markets_groups"
        esi_path_object = "Market.get_markets_groups_market_group_id"
        field_mappings = {"parent_market_group": "parent_group_id"}
        children = {"types": "EveType"}


class EveMoon(EveUniverseEntityModel):
    """"moon in Eve Online"""

    eve_planet = models.ForeignKey(
        "EvePlanet", on_delete=models.CASCADE, related_name="eve_moons"
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )

    objects = EvePlanetChildrenManager("moons")

    class EveUniverseMeta:
        esi_pk = "moon_id"
        esi_path_object = "Universe.get_universe_moons_moon_id"
        field_mappings = {
            "eve_planet": "planet_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }


class EvePlanet(EveUniverseEntityModel):
    """"planet in Eve Online"""

    eve_solar_system = models.ForeignKey(
        "EveSolarSystem", on_delete=models.CASCADE, related_name="eve_planets"
    )
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="eve_planets"
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )

    objects = EvePlanetManager()

    class EveUniverseMeta:
        esi_pk = "planet_id"
        esi_path_object = "Universe.get_universe_planets_planet_id"
        field_mappings = {
            "eve_solar_system": "system_id",
            "eve_type": "type_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }
        children = {"moons": "EveMoon", "asteroid_belts": "EveAsteroidBelt"}

    @classmethod
    def children(cls) -> dict:
        children = dict()

        if EVEUNIVERSE_LOAD_ASTEROID_BELTS:
            children["asteroid_belts"] = "EveAsteroidBelt"

        if EVEUNIVERSE_LOAD_MOONS:
            children["moons"] = "EveMoon"

        return children


class EveRace(EveUniverseEntityModel):
    """"faction in Eve Online"""

    alliance_id = models.PositiveIntegerField(db_index=True)
    description = models.TextField()

    class EveUniverseMeta:
        esi_pk = "race_id"
        esi_path_list = "Universe.get_universe_races"
        esi_path_object = "Universe.get_universe_races"


class EveRegion(EveUniverseEntityModel):
    """region in Eve Online"""

    description = models.TextField(default="")

    class EveUniverseMeta:
        esi_pk = "region_id"
        esi_path_list = "Universe.get_universe_regions"
        esi_path_object = "Universe.get_universe_regions_region_id"
        children = {"constellations": "EveConstellation"}

    @property
    def dotlan_url(self):
        return dotlan.region_url(self.name)

    @property
    def zkb_url(self):
        return zkillboard.region_url(self.id)


class EveSolarSystem(EveUniverseEntityModel):
    """solar system in Eve Online"""

    eve_constellation = models.ForeignKey(
        "EveConstellation", on_delete=models.CASCADE, related_name="eve_solarsystems"
    )
    eve_star = models.OneToOneField(
        "EveStar",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_solarsystem",
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )
    security_status = models.FloatField()

    class EveUniverseMeta:
        esi_pk = "system_id"
        esi_path_list = "Universe.get_universe_systems"
        esi_path_object = "Universe.get_universe_systems_system_id"
        field_mappings = {
            "eve_constellation": "constellation_id",
            "eve_star": "star_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }
        children = {}

    @property
    def is_high_sec(self):
        return self.security_status > 0.5

    @property
    def is_low_sec(self):
        return 0 < self.security_status <= 0.5

    @property
    def is_null_sec(self):
        return self.security_status <= 0 and not self.is_w_space

    @property
    def is_w_space(self):
        return 31000000 <= self.id < 32000000

    @property
    def dotlan_url(self):
        return dotlan.solar_system_url(self.name)

    @property
    def zkb_url(self):
        return zkillboard.solar_system_url(self.id)

    @classmethod
    def children(cls) -> dict:
        children = dict()

        if EVEUNIVERSE_LOAD_PLANETS:
            children["planets"] = "EvePlanet"

        if EVEUNIVERSE_LOAD_STARGATES:
            children["stargates"] = "EveStargate"

        if EVEUNIVERSE_LOAD_STATIONS:
            children["stations"] = "EveStation"

        return children

    @classmethod
    def _disabled_fields(cls) -> set:
        if not EVEUNIVERSE_LOAD_STARS:
            return {"eve_star"}
        else:
            return {}

    def distance_to(self, destination: object) -> float:
        """return the distance in meters to the given solar system
        
        Will return None if one of the systems is in WH space
        """
        if self.is_w_space or destination.is_w_space:
            return None
        else:
            return math.sqrt(
                (destination.position_x - self.position_x) ** 2
                + (destination.position_y - self.position_y) ** 2
                + (destination.position_z - self.position_z) ** 2
            )

    def route_to(self, destination: object, exclude_high_sec: bool = False) -> list:
        """returns the shortest route to given solar system in jumps

        Result returned as list of solar systems incl. origin and destination
        or None if there is no route
        """
        path_ids = self._shortest_path_to(destination, exclude_high_sec)
        if path_ids:
            return [
                EveSolarSystem.objects.get(id=solar_system_id)
                for solar_system_id in path_ids
            ]
        else:
            return None

    def jumps_to(self, destination: object, exclude_high_sec: bool = False) -> int:
        """returns the shortest number of jumps to given solar system

        return None if there is no route
        """
        path_ids = self._shortest_path_to(destination, exclude_high_sec)
        return len(path_ids) - 1 if path_ids else None

    def _shortest_path_to(self, destination: object, exclude_high_sec: bool) -> list:
        """return shortest patch as list of IDs to given solar system
        or empty list if not path exists
        """

        def jumps() -> models.QuerySet:
            return EveStargate.objects.filter(
                destination_eve_solar_system__isnull=False
            ).values_list("eve_solar_system_id", "destination_eve_solar_system_id")

        def jumps_excluding_high_sec() -> models.QuerySet:
            return (
                EveStargate.objects.filter(
                    destination_eve_solar_system__isnull=False,
                    eve_solar_system__security_status__lt=0.5,
                    destination_eve_solar_system__security_status__lt=0.5,
                )
                .select_related("eve_solar_system", "destination_eve_solar_system")
                .values_list("eve_solar_system_id", "destination_eve_solar_system_id")
            )

        cache_route_key = (
            f"EVESDE_ROUTE_{self.id}_{destination.id}_" f"{exclude_high_sec}"
        )
        if self.is_w_space or destination.is_w_space:
            return []

        path = cache.get(cache_route_key)
        if path is not None:
            return path

        else:
            g = nx.Graph()
            if exclude_high_sec:
                jumps_qs = cache.get_or_set(
                    "EVESDE_STARGATES_NO_HIGHSEC",
                    jumps_excluding_high_sec,
                    ROUTE_CACHE_DURATION,
                )
            else:
                jumps_qs = cache.get_or_set(
                    "EVESDE_STARGATES", jumps, ROUTE_CACHE_DURATION
                )

            for jump in jumps_qs:
                g.add_edge(jump[0], jump[1])

            try:
                path = nx.shortest_path(g, self.id, destination.id)
            except (NetworkXNoPath, NodeNotFound):
                path = []

            cache.set(key=cache_route_key, value=path, timeout=ROUTE_CACHE_DURATION)

        return path


class EveStar(EveUniverseEntityModel):
    """"Star in Eve Online"""

    age = models.BigIntegerField()
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="eve_stars"
    )
    luminosity = models.FloatField()
    radius = models.PositiveIntegerField()
    spectral_class = models.CharField(max_length=16)
    temperature = models.PositiveIntegerField()

    class EveUniverseMeta:
        esi_pk = "star_id"
        esi_path_object = "Universe.get_universe_stars_star_id"
        field_mappings = {"eve_type": "type_id"}


class EveStargate(EveUniverseEntityModel):
    """"Stargate in Eve Online"""

    destination_eve_stargate = models.OneToOneField(
        "EveStargate", on_delete=models.SET_DEFAULT, null=True, default=None, blank=True
    )
    destination_eve_solar_system = models.ForeignKey(
        "EveSolarSystem",
        on_delete=models.SET_DEFAULT,
        null=True,
        default=None,
        blank=True,
        related_name="destination_eve_stargates",
    )
    eve_solar_system = models.ForeignKey(
        "EveSolarSystem", on_delete=models.CASCADE, related_name="eve_stargates"
    )
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="eve_stargates"
    )
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )

    objects = EveStargateManager()

    class EveUniverseMeta:
        esi_pk = "stargate_id"
        esi_path_object = "Universe.get_universe_stargates_stargate_id"
        field_mappings = {
            "destination_eve_stargate": ("destination", "stargate_id"),
            "destination_eve_solar_system": ("destination", "system_id"),
            "eve_solar_system": "system_id",
            "eve_type": "type_id",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }
        dont_create_related = {
            "destination_eve_stargate",
            "destination_eve_solar_system",
        }


class EveStation(EveUniverseEntityModel):
    """"station in Eve Online"""

    eve_race = models.ForeignKey(
        "EveRace",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_stations",
    )
    eve_solar_system = models.ForeignKey(
        "EveSolarSystem", on_delete=models.CASCADE, related_name="eve_stations",
    )
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="eve_stations",
    )
    max_dockable_ship_volume = models.FloatField()
    office_rental_cost = models.FloatField()
    owner_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    position_x = models.FloatField(
        null=True, default=None, blank=True, help_text="x position in the solar system"
    )
    position_y = models.FloatField(
        null=True, default=None, blank=True, help_text="y position in the solar system"
    )
    position_z = models.FloatField(
        null=True, default=None, blank=True, help_text="z position in the solar system"
    )
    reprocessing_efficiency = models.FloatField()
    reprocessing_stations_take = models.FloatField()
    services = models.ManyToManyField("EveStationService")

    objects = EveStationManager()

    class EveUniverseMeta:
        esi_pk = "station_id"
        esi_path_object = "Universe.get_universe_stations_station_id"
        field_mappings = {
            "eve_race": "race_id",
            "eve_solar_system": "system_id",
            "eve_type": "type_id",
            "owner_id": "owner",
            "position_x": ("position", "x"),
            "position_y": ("position", "y"),
            "position_z": ("position", "z"),
        }
        inline_objects = {"services": "EveStationService"}


class EveStationService(models.Model):
    """A service in a station"""

    name = models.CharField(max_length=50, unique=True)


class EveType(EveUniverseEntityModel):
    """Type in Eve Online"""

    capacity = models.FloatField(default=None, null=True)
    eve_group = models.ForeignKey(
        "EveGroup", on_delete=models.CASCADE, related_name="eve_types",
    )
    eve_graphic = models.ForeignKey(
        "EveGraphic",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_types",
    )
    icon_id = models.PositiveIntegerField(default=None, null=True, db_index=True)
    eve_market_group = models.ForeignKey(
        "EveMarketGroup",
        on_delete=models.SET_DEFAULT,
        default=None,
        null=True,
        related_name="eve_types",
    )
    mass = models.FloatField(default=None, null=True)
    packaged_volume = models.FloatField(default=None, null=True)
    portion_size = models.PositiveIntegerField(default=None, null=True)
    radius = models.FloatField(default=None, null=True)
    published = models.BooleanField()
    volume = models.FloatField(default=None, null=True)

    class EveUniverseMeta:
        esi_pk = "type_id"
        esi_path_list = "Universe.get_universe_types"
        esi_path_object = "Universe.get_universe_types_type_id"
        field_mappings = {
            "eve_graphic": "graphic_id",
            "eve_group": "group_id",
            "eve_market_group": "market_group_id",
        }
        inline_objects = {
            "dogma_attributes": "EveTypeDogmaAttribute",
            "dogma_effects": "EveTypeDogmaEffect",
        }

    def icon_url(self, size=EveUniverseEntityModel.DEFAULT_ICON_SIZE) -> str:
        """return an image URL to this type as icon"""
        return eveimageserver.type_icon_url(self.id, size=size)

    def render_url(self, size=EveUniverseEntityModel.DEFAULT_ICON_SIZE) -> str:
        """return an image URL to this type as render"""
        return eveimageserver.type_render_url(self.id, size=size)

    @classmethod
    def _disabled_fields(cls) -> set:
        disabled_fields = set()
        if not EVEUNIVERSE_LOAD_GRAPHICS:
            disabled_fields.add("eve_graphic")

        if not EVEUNIVERSE_LOAD_MARKET_GROUPS:
            disabled_fields.add("eve_market_group")

        return disabled_fields

    @classmethod
    def inline_objects(cls) -> dict:
        if EVEUNIVERSE_LOAD_DOGMAS:
            return super().inline_objects()
        else:
            return dict()


class EveTypeDogmaAttribute(EveUniverseInlineModel):
    """Dogma attribute in Eve Online"""

    eve_dogma_attribute = models.ForeignKey(
        "EveDogmaAttribute",
        on_delete=models.CASCADE,
        related_name="eve_type_dogma_attributes",
    )
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="dogma_attributes"
    )
    value = models.FloatField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["eve_type", "eve_dogma_attribute"], name="functional PK"
            )
        ]

    class EveUniverseMeta:
        parent_fk = "eve_type"
        functional_pk = [
            "eve_type",
            "eve_dogma_attribute",
        ]
        field_mappings = {"eve_dogma_attribute": "attribute_id"}

    def __repr__(self) -> str:
        return (
            f"EveTypeDogmaAttributes(eve_type='{self.eve_type}', "
            f"eve_dogma_attribute={self.eve_dogma_attribute}, "
            f"value={self.value})"
        )


class EveTypeDogmaEffect(EveUniverseInlineModel):
    """Dogma effect in Eve Online"""

    eve_dogma_effect = models.ForeignKey(
        "EveDogmaEffect",
        on_delete=models.CASCADE,
        related_name="eve_type_dogma_effects",
    )
    eve_type = models.ForeignKey(
        "EveType", on_delete=models.CASCADE, related_name="dogma_effects"
    )
    is_default = models.BooleanField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["eve_type", "eve_dogma_effect"], name="functional PK"
            )
        ]

    class EveUniverseMeta:
        parent_fk = "eve_type"
        functional_pk = [
            "eve_type",
            "eve_dogma_effect",
        ]
        field_mappings = {"eve_dogma_effect": "effect_id"}

    def __repr__(self) -> str:
        return (
            f"EveTypeDogmaEffect("
            f"eve_type='{self.eve_type}', "
            f"eve_dogma_effect={self.eve_dogma_effect}, "
            f"is_default={self.is_default})"
        )


class EveUnit(EveUniverseEntityModel):
    """Units in Eve Online"""

    display_name = models.CharField(max_length=50, default="")
    description = models.TextField(default="")

    objects = models.Manager()

    class EveUniverseMeta:
        esi_pk = "unit_id"
        esi_path_object = None
        field_mappings = {
            "unit_id": "id",
            "unit_name": "name",
        }


class EveEntity(EveUniverseEntityModel):
    """An entity object if Eve Online like a character or a corporation"""

    ZKB_ENTITY_URL_BASE = "https://zkillboard.com/"

    CATEGORY_ALLIANCE = "alliance"
    CATEGORY_CHARACTER = "character"
    CATEGORY_CONSTELLATION = "constellation"
    CATEGORY_CORPORATION = "corporation"
    CATEGORY_FACTION = "faction"
    CATEGORY_INVENTORY_TYPE = "inventory_type"
    CATEGORY_REGION = "region"
    CATEGORY_SOLAR_SYSTEM = "solar_system"
    CATEGORY_STATION = "station"

    CATEGORY_CHOICES = (
        (CATEGORY_ALLIANCE, "alliance"),
        (CATEGORY_CHARACTER, "character"),
        (CATEGORY_CONSTELLATION, "constellation"),
        (CATEGORY_CORPORATION, "corporation"),
        (CATEGORY_FACTION, "faction"),
        (CATEGORY_INVENTORY_TYPE, "inventory_type"),
        (CATEGORY_REGION, "region"),
        (CATEGORY_SOLAR_SYSTEM, "solar_system"),
        (CATEGORY_STATION, "station"),
    )

    category = models.CharField(
        max_length=16, choices=CATEGORY_CHOICES, default=None, null=True
    )

    objects = EveEntityManager()

    class EveUniverseMeta:
        esi_pk = "ids"
        esi_path_object = "Universe.post_universe_names"

    def __str__(self):
        if self.name:
            return self.name
        else:
            return f"ID:{self.id}"

    def __repr__(self):
        return (
            f"{type(self).__name__}(id={self.id}, name='{self.name}', "
            f"category='{self.category}')"
        )

    @property
    def zkb_url(self) -> str:
        """return zkb link for this entity if one exists, else empty string"""
        map_category_2_other = {
            self.CATEGORY_ALLIANCE: "alliance_url",
            self.CATEGORY_CHARACTER: "character_url",
            self.CATEGORY_CORPORATION: "corporation_url",
            self.CATEGORY_REGION: "region_url",
            self.CATEGORY_SOLAR_SYSTEM: "solar_system_url",
        }
        if self.category not in map_category_2_other:
            return ""
        else:
            func = map_category_2_other[self.category]
            return getattr(zkillboard, func)(self.id)

    @property
    def dotlan_url(self) -> str:
        """return dotlan link for this entity if one exists, else empty string"""
        if not self.name:
            return ""

        map_category_2_other = {
            self.CATEGORY_ALLIANCE: "alliance_url",
            self.CATEGORY_CORPORATION: "corporation_url",
            self.CATEGORY_REGION: "region_url",
            self.CATEGORY_SOLAR_SYSTEM: "solar_system_url",
        }
        if self.category not in map_category_2_other:
            return ""
        else:
            func = map_category_2_other[self.category]
            return getattr(dotlan, func)(self.name)

    @transaction.atomic
    def update_from_esi(self):
        obj, _ = EveEntity.objects.update_or_create_esi(id=self.id)
        return obj

    def icon_url(self, size: int = EveUniverseEntityModel.DEFAULT_ICON_SIZE) -> str:
        map_category_2_other = {
            self.CATEGORY_ALLIANCE: "alliance_logo_url",
            self.CATEGORY_CHARACTER: "character_portrait_url",
            self.CATEGORY_CORPORATION: "corporation_logo_url",
            self.CATEGORY_INVENTORY_TYPE: "type_icon_url",
        }
        if self.category not in map_category_2_other:
            return ""
        else:
            func = map_category_2_other[self.category]
            return getattr(eveimageserver, func)(self.id, size=size)

    def get_or_create_pendant_object(
        self, *, include_children: bool = False, wait_for_children: bool = True,
    ) -> tuple:
        """returns the pendant object for this entity along with a created flag,
        e.g. EveSolarSystem for an entity with category "solar system"
        """
        map_category_2_other = {
            self.CATEGORY_ALLIANCE: (EveAllianceInfo, "create_alliance"),
            self.CATEGORY_CHARACTER: (EveCharacter, "create_character"),
            self.CATEGORY_CORPORATION: (EveCorporationInfo, "create_corporation"),
            self.CATEGORY_INVENTORY_TYPE: (EveType, None),
            self.CATEGORY_REGION: (EveRegion, None),
            self.CATEGORY_SOLAR_SYSTEM: (EveSolarSystem, None),
        }
        if self.category not in map_category_2_other:
            raise NotImplementedError()
        else:
            MyModel, func = map_category_2_other[self.category]
            if func:
                try:
                    obj = MyModel.objects.get(id=self.id)
                    created = False
                except MyModel.DoesNotExist:
                    obj = getattr(MyModel.objects, func)(self.id)
                    created = True
                return obj, created
            else:
                return MyModel.objects.get_or_create_esi(
                    id=self.id,
                    include_children=include_children,
                    wait_for_children=wait_for_children,
                )

