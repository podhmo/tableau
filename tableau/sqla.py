from sqlalchemy.schema import Table
from tableau.containers import DatumBase
from tableau.declarations import DynamicField, Lazy, one_to_many, many_to_one, many_to_many, auto
from tableau.utils import string_container_from_value, is_iterable_container
from sqlalchemy.orm.properties import RelationshipProperty
from types import FunctionType
from warnings import warn
from weakref import WeakKeyDictionary

mixin_class_registry = WeakKeyDictionary()

def cleanup():
    for declarative, sadatum_class in mixin_class_registry.items():
        del sadatum_class._decl_class_registry[sadatum_class.__name__]

def newSADatum(metadata, base=None):
    table_to_declarative = {}
    if base is not None:
        for class_name, declarative in base._decl_class_registry.items():
            table_to_declarative[declarative.__table__.name] = declarative

    def is_declarative(cls):
        for base in cls.__mro__:
            if hasattr(base, '_decl_class_registry'):
                return True
        return False

    def default_value(column_def):
        if column_def.default is None:
            return None
        if not column_def.default.is_scalar:
            warn("non-scalar default value is not supported")
            return None
        return column_def.default.arg

    class SADatum(DatumBase):
        _tableau_declarative = None
        _tableau_table = None

        @classmethod
        def _tableau_lookup_mixin_class(cls, declarative):
            class_name = "SADatum#%s" % declarative.__name__
            retval = mixin_class_registry.get(declarative)
            if retval is None:
                retval = type(class_name, (cls, declarative), {
                    '_tableau_declarative': declarative,
                    })
                for prop in declarative.__mapper__.iterate_properties:
                    # XXX: forcefully turn off typechecks to
                    # let SQLAlchemy treat the subclass of a declarative class
                    # as its base
                    if isinstance(prop, RelationshipProperty):
                        prop._dependency_processor.enable_typechecks = False
                mixin_class_registry[declarative] = retval
            return retval

        def __new__(cls, schema, id_fields=None, **fields):
            if isinstance(schema, basestring):
                table = metadata.tables.get(schema, None)
                if table is None:
                    raise ValueError("%s is not defined in the metadata" % schema)
                declarative = None
            elif isinstance(schema, Table):
                table = schema
                declarative = None
            elif base is not None and is_declarative(schema):
                declarative = schema
                table = schema.__table__
            else:
                raise TypeError("schema must be either a table name or a %s instance" % Table.__name__)
            if base is not None and declarative is None:
                declarative = table_to_declarative.get(schema)
                if declarative is None:
                    raise ValueError("declarative class for %s is not in the class registry" % schema)

            if declarative is not None:
                assert not isinstance(declarative, SADatum)
                _cls = cls._tableau_lookup_mixin_class(declarative)
            else:
                _cls = cls
            newinstance = object.__new__(_cls)
            newinstance._tableau_table = table
            return newinstance

        def __init__(self, schema, id_fields=None, **fields):
            columns = self._tableau_table.columns

            _fields = dict((k, default_value(columns[k])) for k in columns.keys())
            _fields.update(fields)
            primary_key_columns = self._tableau_table.primary_key.columns.keys()
            if id_fields is not None:
                if isinstance(id_fields, basestring):
                    id_fields = (id_fields, )
                elif isinstance(id_fields, tuple):
                    id_fields = id_fields
                else:
                    id_fields = tuple(id_fields)
                if len(primary_key_columns) != len(id_fields):
                    id_fields_matched = False
                else:
                    id_fields_matched = True
                    for k1, k2 in zip(primary_key_columns, id_fields):
                        if k1 != k2:
                            id_fields_matched = False
                            break
                if not id_fields_matched:
                    raise ValueError('id_fields does not match to the table definition ([%s] != [%s])' % (','.join(id_fields), ','.join(primary_key_columns)))
            else:
                if len(primary_key_columns) == 1 and \
                    self._tableau_table.primary_key.columns[primary_key_columns[0]].autoincrement:
                    id_fields = auto(primary_key_columns[0])
                else:
                    id_fields = tuple(primary_key_columns)
            self._tableau_schema = self._tableau_table.name
            self._tableau_id_fields = id_fields
            self._tableau_fields = {}
            for k, v in _fields.iteritems():
                setattr(self, k, v)

        def __check_key_is_declared(self, k):
            if k not in self._tableau_table.columns and (base is None or not self.__class__.__mapper__.has_property(k)):
                raise KeyError("%s is not declared in the table definition or mapper configuration" % k)

        def _value_of(self, k, value):
            if isinstance(value, one_to_many) and base is not None:
                prop = self.__class__.__mapper__.get_property(k) 
                if prop.uselist:
                    return value()
                else:
                    return value()[0]
            elif isinstance(value, DynamicField):
                return value()
            else:
                return value

        def __getattribute__(self, k):
            if k.startswith('_') or base is not None:
                return object.__getattribute__(self, k)
            try:
                return object.__getattribute__(self, '_tableau_fields')[k]
            except KeyError:
                raise AttributeError('%s.%s' % (self._tableau_schema, k))
                
        def __setattr__(self, k, v):
            if k.startswith('_'):
                object.__setattr__(self, k, v)
            else:
                if isinstance(v, FunctionType):
                    v = Lazy(v)
                elif isinstance(v, DatumBase):
                    # implicit many_to_one
                    v = many_to_one(v, k, v._tableau_id_fields)
                elif is_iterable_container(v):
                    # implicit one_to_many
                    v = one_to_many(v, k)
                if isinstance(v, DynamicField):
                    v.bind(self, k)

                if isinstance(v, many_to_one):
                    for _k in v.this_side_fields:
                        self.__check_key_is_declared(_k)
                elif isinstance(v, one_to_many):
                    self.__check_key_is_declared(k)
                    if v.referred_fields is not None:
                        for _k in v.referred_fields:
                            self.__check_key_is_declared(_k)
                object.__getattribute__(self, '_tableau_fields')[k] = v
                if self._tableau_declarative is not None and not isinstance(v, Lazy):
                    object.__setattr__(self, k, self._value_of(k, v))

        def _tableau_on_fixation(self):
            for k, v in self._tableau_fields.items():
                if self._tableau_declarative is not None and isinstance(v, Lazy):
                    self._tableau_declarative.__setattr__(self, k, self._value_of(k, v))

    return SADatum
