from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import six
from copy import deepcopy

import numpy as np
import pandas as pd
import matplotlib.cbook as cbook
import pandas.core.common as com
from patsy.eval import EvalEnvironment

from .utils.exceptions import GgplotError
from .utils import DISCRETE_KINDS, ninteraction, suppress
from .utils import check_required_aesthetics, defaults
from .aes import aes, is_calculated_aes, strip_dots

_TPL_EVAL_FAIL = """\
Could not evaluate the '{}' mapping: '{}' \
(original error: {})"""

_TPL_BAD_EVAL_TYPE = """\
The '{}' mapping: '{}' produced a value of type '{}',\
but only single items and lists/arrays can be used. \
(original error: {})"""


class Layers(list):
    """
    List of layers

    Each layer knows its position/zorder (1 based) in the list.
    """
    def append(self, item):
        item.zorder = len(self) + 1
        return list.append(self, item)

    def _set_zorder(self, other):
        for i, item in enumerate(other, start=len(self)+1):
            item.zorder = i
        return other

    def extend(self, other):
        other = self._set_zorder(other)
        return list.extend(self, other)

    def __iadd__(self, other):
        other = self._set_zorder(other)
        return list.__iadd__(self, other)

    def __add__(self, other):
        other = self._set_zorder(other)
        return list.__add__(self, other)

    @property
    def data(self):
        return [l.data for l in self]

    def setup_data(self):
        for l in self:
            l.setup_data()

    def draw(self, panel, coord):
        for l in self:
            l.draw(panel, coord)

    def compute_aesthetics(self, plot):
        for l in self:
            l.compute_aesthetics(plot)

    def compute_statistic(self, panel):
        for l in self:
            l.compute_statistic(panel)

    def map_statistic(self, plot):
        for l in self:
            l.map_statistic(plot)

    def compute_position(self, panel):
        for l in self:
            l.compute_position(panel)

    def use_defaults(self):
        for l in self:
            l.use_defaults()

    def transform(self, scales):
        for l in self:
            l.data = scales.transform_df(l.data)

    def train(self, scales):
        for l in self:
            l.data = scales.train_df(l.data)

    def map(self, scales):
        for l in self:
            l.data = scales.map_df(l.data)


class layer(object):

    def __init__(self, geom=None, stat=None,
                 data=None, mapping=None,
                 position=None, inherit_aes=True,
                 show_legend=None):
        self.geom = geom
        self.stat = stat
        self.data = data
        self.mapping = mapping
        self.position = position
        self.inherit_aes = inherit_aes
        self.show_legend = show_legend
        self._active_mapping = {}
        self.zorder = 0

    @staticmethod
    def from_geom(geom):
        """
        Create a layer given a :class:`geom`

        Parameters
        ----------
        geom : geom
            `geom` from which a layer will be created

        Returns
        -------
        out : layer
            Layer that represents the specific `geom`.
        """
        kwargs = geom._kwargs
        lkwargs = {'geom': geom,
                   'mapping': geom.mapping,
                   'data': geom.data,
                   'stat': geom._stat,
                   'position': geom._position}

        for param in ('show_legend', 'inherit_aes'):
            if param in kwargs:
                lkwargs[param] = kwargs[param]
            elif param in geom.DEFAULT_PARAMS:
                lkwargs[param] = geom.DEFAULT_PARAMS[param]

        return layer(**lkwargs)

    def __deepcopy__(self, memo):
        """
        Deep copy without copying the self.data dataframe
        """
        cls = self.__class__
        result = cls.__new__(cls)
        memo[id(self)] = result
        old = self.__dict__
        new = result.__dict__

        for key, item in old.items():
            if key == 'data':
                new[key] = old[key]
            else:
                new[key] = deepcopy(old[key], memo)

        return result

    def layer_mapping(self, mapping):
        """
        Return the mappings that are active in this layer

        Parameters
        ----------
        mapping : aes
            mappings in the ggplot call

        Note
        ----
        Once computed the layer mappings are also stored
        in self._active_mapping
        """
        # For certain geoms, it is useful to be able to
        # ignore the default aesthetics and only use those
        # set in the layer
        if self.inherit_aes:
            aesthetics = defaults(self.mapping, mapping)
        else:
            aesthetics = self.mapping

        # drop aesthetic parameters or the calculated aesthetics
        calculated = set(is_calculated_aes(aesthetics))
        d = dict((ae, v) for ae, v in aesthetics.items()
                 if (ae not in self.geom.aes_params) and
                 (ae not in calculated))
        self._active_mapping = aes(**d)
        return self._active_mapping

    def compute_aesthetics(self, plot):
        """
        Return a dataframe where the columns match the
        aesthetic mappings.

        Transformations like 'factor(cyl)' and other
        expression evaluation are  made in here
        """
        data = self.data
        aesthetics = self.layer_mapping(plot.mapping)

        # Override grouping if set in layer.
        with suppress(KeyError):
            aesthetics['group'] = self.geom.aes_params['group']

        env = EvalEnvironment.capture(eval_env=plot.environment)
        env = env.with_outer_namespace({'factor': pd.Categorical})

        # Using `type` preserves the subclass of pd.DataFrame
        evaled = type(data)(index=data.index)
        has_aes_params = False  # aesthetic parameter in aes()

        # If a column name is not in the data, it is evaluated/transformed
        # in the environment of the call to ggplot
        for ae, col in aesthetics.items():
            if isinstance(col, six.string_types):
                if col in data:
                    evaled[ae] = data[col]
                else:
                    try:
                        new_val = env.eval(col, inner_namespace=data)
                    except Exception as e:
                        raise GgplotError(
                            _TPL_EVAL_FAIL.format(ae, col, str(e)))

                    try:
                        evaled[ae] = new_val
                    except Exception as e:
                        raise GgplotError(
                            _TPL_BAD_EVAL_TYPE.format(
                                ae, col, str(type(new_val)), str(e)))
            elif com.is_list_like(col):
                n = len(col)
                if len(data) and n != len(data) and n != 1:
                    raise GgplotError(
                        "Aesthetics must either be length one, " +
                        "or the same length as the data")
                elif n == 1:
                    col = col[0]
                has_aes_params = True
                evaled[ae] = col
            elif not cbook.iterable(col) and cbook.is_numlike(col):
                evaled[ae] = col
            else:
                msg = "Do not know how to deal with aesthetic '{}'"
                raise GgplotError(msg.format(ae))

        # int columns are continuous, cast them to floats.
        # Also when categoricals are mapped onto scales,
        # they create int columns.
        # Some stats e.g stat_bin need this distinction
        for col in evaled:
            if evaled[col].dtype == np.int:
                evaled[col] = evaled[col].astype(np.float)

        evaled_aes = aes(**dict((col, col) for col in evaled))
        plot.scales.add_defaults(evaled, evaled_aes)

        if len(data) == 0 and has_aes_params:
            # No data, and vectors suppled to aesthetics
            evaled['PANEL'] = 1
        else:
            evaled['PANEL'] = data['PANEL']

        self.data = add_group(evaled)

    def compute_statistic(self, panel):
        """
        Compute & return statistics for this layer
        """
        data = self.data
        if not len(data):
            return type(data)()

        params = self.stat.setup_params(data)
        data = self.stat.use_defaults(data)
        data = self.stat.setup_data(data)
        data = self.stat.compute_layer(data, params, panel)
        self.data = data

    def map_statistic(self, plot):
        """
        Mapping aesthetics to computed statistics
        """
        data = self.data
        if not len(data):
            return type(data)()

        # Assemble aesthetics from layer, plot and stat mappings
        aesthetics = deepcopy(self.mapping)
        if self.inherit_aes:
            aesthetics = defaults(aesthetics, plot.mapping)

        aesthetics = defaults(aesthetics, self.stat.DEFAULT_AES)

        # The new aesthetics are those that the stat calculates
        # and have been mapped to with dot dot notation
        # e.g aes(y='..count..'), y is the new aesthetic and
        # 'count' is the computed column in data
        new = {}  # {'aesthetic_name': 'calculated_stat'}
        stat_data = type(data)()
        for ae in is_calculated_aes(aesthetics):
            new[ae] = strip_dots(aesthetics[ae])
            # In conjuction with the pd.concat at the end,
            # be careful not to create duplicate columns
            # for cases like y='..y..'
            if ae != new[ae]:
                stat_data[ae] = data[new[ae]]

        if not new:
            return data

        # Add any new scales, if needed
        plot.scales.add_defaults(data, new)

        # Transform the values, if the scale say it's ok
        # (see stat_spoke for one exception)
        if self.stat.retransform:
            stat_data = plot.scales.transform_df(stat_data)

        self.data = pd.concat([data, stat_data], axis=1)

    def setup_data(self):
        """
        Prepare/modify data for plotting
        """
        data = self.data
        if len(data) == 0:
            return type(data)()

        data = self.geom.setup_data(data)

        check_required_aesthetics(
            self.geom.REQUIRED_AES,
            set(data.columns) | set(self.geom.aes_params),
            self.geom.__class__.__name__)

        self.data = data

    def compute_position(self, panel):
        """
        Compute the position of each geometric object
        in concert with the other objects in the panel
        """
        params = self.position.setup_params(self.data)
        data = self.position.setup_data(self.data, params)
        data = self.position.compute_layer(data, params, panel)
        self.data = data

    def draw(self, panel, coord):
        """
        Draw geom

        Parameters
        ----------
        panel : Panel
            Panel object created when the plot is getting
            built
        coord : coord
            Type of coordinate axes
        """
        params = deepcopy(self.geom.params)
        params.update(self.stat.params)
        params['zorder'] = self.zorder
        # At this point each layer must have the data
        # that is created by the plot build process
        self.geom.draw_layer(self.data, panel, coord, **params)

    def use_defaults(self, data=None):
        """
        Prepare/modify data for plotting
        """
        if data is None:
            data = self.data
        return self.geom.use_defaults(data)


NO_GROUP = -1


def add_group(data):
    if len(data) == 0:
        return data

    if 'group' not in data:
        disc = discrete_columns(data, ignore=['label'])
        if disc:
            data['group'] = ninteraction(data[disc], drop=True)
        else:
            data['group'] = NO_GROUP
    else:
        data['group'] = ninteraction(data[['group']], drop=True)

    return data


def discrete_columns(df, ignore):
    """
    Return a list of the discrete columns in the
    dataframe `df`. `ignore` is a list|set|tuple with the
    names of the columns to skip.
    """
    lst = []
    for col in df:
        if (df[col].dtype.kind in DISCRETE_KINDS) and (col not in ignore):
            # Some columns are represented as object dtype
            # but may have compound structures as values.
            try:
                hash(df[col].iloc[0])
            except TypeError:
                continue
            lst.append(col)
    return lst
