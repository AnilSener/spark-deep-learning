#
# Copyright 2017 Databricks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import joblib as jl
import logging
from pathlib import Path
import shutil
import six
from tempfile import mkdtemp

import keras.backend as K
from keras.models import Model as KerasModel, load_model
import tensorflow as tf
import tensorframes.core as tfrm

from ..utils import jvmapi as JVMAPI
from ..utils import graph_utils as tfx

logger = logging.getLogger('sparkdl')

class IsolatedSession(object):
    """
    Building TensorFlow graph and export as either UDF or GraphFunction

    It provides a GraphBuilder object which provides
    - importing existing GraphFunction object as a subgraph
    - exporting current graph as an GraphFunction object

    This is a thin layer on top of `tf.Session`.

    :param g: Graph, use the provided TensorFlow graph as default graph
    :param keras: bool, whether to also let Keras TensorFlow backend use this session
    """
    def __init__(self, graph=None, keras=False):
        self.graph = graph or tf.Graph()
        self.sess = tf.Session(graph=self.graph)
        if keras:
            self.keras_prev_sess = K.get_session()
        else:
            self.keras_prev_sess = None

    def __enter__(self):
        self.sess.as_default()
        self.sess.__enter__()
        if self.keras_prev_sess is not None:
            K.set_session(self.sess)
        return self

    def __exit__(self, *args):
        if self.keras_prev_sess is not None:
            K.set_session(self.keras_prev_sess)
        self.sess.__exit__(*args)

    def run(self, *args, **kargs):
        return self.sess.run(*args, **kargs)

    def asGraphFunction(self, inputs, outputs, strip_and_freeze=True):
        """
        Export the graph in this session as a GraphFunction object

        :param inputs: list, graph elements representing the inputs
        :param outputs: list, graph elements representing the outputs
        :param strip_and_freeze: bool, should we remove unused part of the graph and freee its values
        """
        if strip_and_freeze:
            gdef = tfx.strip_and_freeze_until(outputs, self.graph, self.sess)
        else:
            gdef = self.graph.as_graph_def(add_shapes=True)
        return GraphFunction(graph_def=gdef,
                             input_names=[tfx.validated_input(self.graph, elem) for elem in inputs],
                             output_names=[tfx.validated_output(self.graph, elem) for elem in outputs])

    def import_graph_function(self, gfn, input_map=None, name="GFN-IMPORT", **gdef_kargs):
        """
        Import a GraphFunction object into the current session

        :param gfn: GraphFunction, an object representing a TensorFlow graph and its inputs and outputs
        :param input_map: dict, mapping from input names to existing graph elements
        :param name: str, the scope for all the variables in the GraphFunction's elements
        :param gdef_kargs: other keyword elements for TensorFlow's `import_graph_def`
        """
        try:
            del gdef_kargs["return_elements"]
        except KeyError:
            pass
        if input_map is not None:
            assert set(input_map.keys()) <= set(gfn.input_names), \
                "cannot locate provided input elements in the graph"

        input_names = gfn.input_names
        output_names = gfn.output_names
        if name is not None:
            name = name.strip()
            if len(name) > 0:
                output_names = [
                    name + '/' + op_name for op_name in gfn.output_names]
                input_names = [
                    name + '/' + op_name for op_name in gfn.input_names]

        # When importing, provide the original output op names
        tf.import_graph_def(gfn.graph_def,
                            input_map=input_map,
                            return_elements=gfn.output_names,
                            name=name,
                            **gdef_kargs)
        feeds = [tfx.get_tensor(self.graph, name) for name in input_names]
        fetches = [tfx.get_tensor(self.graph, name) for name in output_names]
        return (feeds, fetches)


class GraphFunction(object):
    """
    Represent a TensorFlow graph with its GraphDef, input and output operation names.

    :param graph_def: GraphDef, a static ProtocolBuffer object holding informations of a TensorFlow graph
    :param input_names: names to the input graph elements (must be of Placeholder type)
    :param output_names: names to the output graph elements
    """

    def __init__(self, graph_def, input_names, output_names):
        """
        :param graph_def: GraphDef object
        :param input_names: list of input (operation) names (must be typed `Placeholder`)
        :param output_names: list of output (operation) names
        """
        self.graph_def = graph_def
        self.input_names = input_names
        self.output_names = output_names

    def dump(self, fpath):
        """
        Store the GraphFunction to a file

        :param fpath: str or path, path to the serialized GraphFunction
        """
        _st = {"graph_def_bytes": self.graph_def.SerializeToString(),
               "inputs": self.input_names,
               "outputs": self.output_names}
        assert isinstance(fpath, six.string_types)
        if not fpath.endswith("jl"):
            fpath += ".jl"
        jl.dump(_st, fpath)

    @classmethod
    def from_file(cls, fpath):
        """
        Load an existing GraphFunction from file.
        This implementation uses `joblib` to provide good I/O performance

        :param fpath: str or path, path to the serialized GraphFunction
        """
        _st = jl.load(fpath)
        assert set(['inputs', 'graph_def_bytes', 'outputs']) <= set(_st.keys())
        gdef = tf.GraphDef.FromString(_st["graph_def_bytes"])  # pylint: disable=E1101
        return cls(graph_def=gdef,
                   input_names=_st["inputs"],
                   output_names=_st["outputs"])


    @classmethod
    def from_keras(cls, model_or_file_path):
        """ Build a GraphFunction from a Keras model
        """
        if isinstance(model_or_file_path, KerasModel):
            model = model_or_file_path
            model_path = Path(mkdtemp(prefix='kera-')) / "model.h5"
            # Save to tempdir and restore in a new session
            model.save(str(model_path), overwrite=True)
            is_temp_model = True
        else:
            model_path = model_or_file_path
            is_temp_model = False

        # Keras load function requires path string
        if not isinstance(model_path, six.string_types):
            model_path = str(model_path)

        with IsolatedSession(keras=True) as issn:
            K.set_learning_phase(0) # Testing phase
            model = load_model(model_path)
            gfn = issn.asGraphFunction(model.inputs, model.outputs)

        if is_temp_model:
            shutil.rmtree(str(Path(model_path).parent), ignore_errors=True)

        return gfn

    @classmethod
    def from_list(cls, functions):
        """
        Takes multiple graph functions and merges them into a single graph function.
        It is assumed that there is only one input and one output in the intermediary layers

        :param functions: a list of tuples (scope name, GraphFunction object).
        """
        assert len(functions) >= 1, ("must provide at least one function", functions)
        if 1 == len(functions):
            return functions[0]
        for (scope_in, gfn_in), (scope_out, gfn_out) in zip(functions[:-1], functions[1:]):
            assert len(gfn_in.output_names) == len(gfn_out.input_names), \
                "graph function link {} -> {} require compatible layers".format(scope_in, scope_out)
            if len(gfn_out.input_names) != 1:
                raise NotImplementedError("Only support single input/output for intermediary layers")

        # Acquire initial placeholders' properties
        with IsolatedSession() as issn:
            _, first_gfn = functions[0]
            feeds, _ = issn.import_graph_function(first_gfn, name='')
            first_input_info = []
            for tnsr in feeds:
                name = tfx.op_name(issn.graph, tnsr)
                first_input_info.append((tnsr.dtype, tnsr.shape, name))

        # Build a linear chain of all the provide functions
        with IsolatedSession() as issn:
            first_inputs = [tf.placeholder(dtype, shape, name)
                            for (dtype, shape, name) in first_input_info]
            prev_outputs = first_inputs

            for idx, (scope, gfn) in enumerate(functions):
                # Give a scope to each function to avoid name conflict
                if scope is None or len(scope.strip()) == 0:
                    scope = 'GFN-BLK-{}'.format(idx)
                _msg = 'merge: stage {}, scope {}'.format(idx, scope)
                logger.info(_msg)
                input_map = dict(zip(gfn.input_names, prev_outputs))
                _, fetches = issn.import_graph_function(
                    gfn, name=scope, input_map=input_map)
                prev_outputs = fetches

            # Add a non-scoped output name as the output node
            last_output_names = functions[-1][1].output_names
            last_outputs = []
            for tnsr, name in zip(prev_outputs, last_output_names):
                last_outputs.append(tf.identity(tnsr, name=name))

            gfn = issn.asGraphFunction(first_inputs, last_outputs)

        return gfn