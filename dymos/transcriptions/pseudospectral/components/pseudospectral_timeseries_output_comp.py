import numpy as np
from scipy.linalg import block_diag
import scipy.sparse as sp

from openmdao.utils.units import unit_conversion

from ....utils.lagrange import lagrange_matrices

from ...common.timeseries_output_comp import TimeseriesOutputCompBase


_USE_SPARSE = True


class PseudospectralTimeseriesOutputComp(TimeseriesOutputCompBase):
    """
    Class definition of the PseudospectralTimeseriesOutputComp.

    Parameters
    ----------
    **kwargs : dict
        Dictionary of optional arguments.
    """
    def __init__(self, **kwargs):
        super(PseudospectralTimeseriesOutputComp, self).__init__(**kwargs)

        # Flag to set if no multiplication by the interpolation matrix is necessary
        self._no_interp = False

    def _declare_options(self):
        """
        Declare options before kwargs are processed in the init method.
        """
        super()._declare_options()
        self.options.declare('time_units', default=None, allow_none=True, types=str,
                             desc='Units of time')

    def setup(self):
        """
        Define the independent variables as output variables.
        """
        igd = self.options['input_grid_data']
        ogd = self.options['output_grid_data']
        output_subset = self.options['output_subset']

        if ogd is None:
            ogd = igd

        if ogd is igd and output_subset == 'all':
            self._no_interp = True

        self.input_num_nodes = igd.num_nodes
        self.output_num_nodes = ogd.subset_num_nodes[output_subset]

        # Build the interpolation matrix which maps from the input grid to the output grid.
        # Rather than a single phase-wide interpolating polynomial, map each segment.
        # To do this, find the nodes in the output grid which fall in each segment of the input
        # grid.  Then build a Lagrange interpolating polynomial for that segment
        L_blocks = []
        D_blocks = []
        output_nodes_ptau = ogd.node_ptau[ogd.subset_node_indices[output_subset]]

        for iseg in range(igd.num_segments):
            i1, i2 = igd.segment_indices[iseg]
            iptau_segi = igd.node_ptau[i1:i2]
            istau_segi = igd.node_stau[i1:i2]

            # The indices of the output grid that fall within this segment of the input grid
            if ogd is igd:
                optau_segi = iptau_segi
            else:
                ptau_hi = igd.segment_ends[iseg+1]
                if iseg < igd.num_segments - 1:
                    optau_segi = output_nodes_ptau[output_nodes_ptau <= ptau_hi]
                else:
                    optau_segi = output_nodes_ptau

                # Remove the captured nodes so we don't accidentally include them again
                output_nodes_ptau = output_nodes_ptau[len(optau_segi):]

            # # Now get the output nodes which fall in iseg in iseg's segment tau space.
            ostau_segi = 2.0 * (optau_segi - iptau_segi[0]) / (iptau_segi[-1] - iptau_segi[0]) - 1

            # Create the interpolation matrix and add it to the blocks
            L, D = lagrange_matrices(istau_segi, ostau_segi)
            L_blocks.append(L)
            D_blocks.append(D)

        if _USE_SPARSE:
            self.interpolation_matrix = sp.block_diag(L_blocks, format='csr')
            self.differentiation_matrix = sp.block_diag(D_blocks, format='csr')
        else:
            self.interpolation_matrix = block_diag(*L_blocks)
            self.differentiation_matrix = block_diag(*D_blocks)

        self.add_input('dt_dstau', shape=(self.input_num_nodes,), units=self.options['time_units'])

    def _add_output_configure(self, name, units, shape, desc='', src=None, rate=False):
        """
        Add a single timeseries output.

        Can be called by parent groups in configure.

        Parameters
        ----------
        name : str
            name of the variable in this component's namespace.
        shape : int or tuple or list or None
            Shape of this variable, only required if val is not an array.
            Default is None.
        units : str or None
            Units in which the output variables will be provided to the component during execution.
            Default is None, which means it has no units.
        desc : str
            description of the timeseries output variable.
        src : str
            The src path of the variables input, used to prevent redundant inputs.
        rate : bool
            If True, timeseries output is a rate.

        Returns
        -------
        bool
            True if a new input was added for the output, or False if it reuses an existing input.
        """
        input_num_nodes = self.input_num_nodes
        output_num_nodes = self.output_num_nodes
        added_source = False

        if name in self._vars:
            return False

        if src in self._sources:
            # If we're already pulling the source into this timeseries, use that as the
            # input for this output.
            input_name = self._sources[src]
            input_units = self._units[input_name]
        else:
            input_name = f'input_values:{name}'
            self.add_input(input_name,
                           shape=(input_num_nodes,) + shape,
                           units=units, desc=desc)
            self._sources[src] = input_name
            input_units = self._units[input_name] = units
            added_source = True

        output_name = name
        self.add_output(output_name, shape=(output_num_nodes,) + shape, units=units, desc=desc)

        self._vars[name] = (input_name, output_name, shape, rate)

        size = np.prod(shape)
        jac = np.zeros((output_num_nodes, size, input_num_nodes, size))

        if rate:
            mat = self.differentiation_matrix
        else:
            mat = self.interpolation_matrix

        for i in range(size):
            if _USE_SPARSE:
                jac[:, i, :, i] = mat.toarray()
            else:
                jac[:, i, :, i] = mat

        jac = jac.reshape((output_num_nodes * size, input_num_nodes * size), order='C')

        jac_rows, jac_cols = np.nonzero(jac)

        # There's a chance that the input for this output was pulled from another variable with
        # different units, so account for that with a conversion.
        if input_units is None or units is None:
            self.declare_partials(of=output_name, wrt=input_name,
                                  rows=jac_rows, cols=jac_cols, val=jac[jac_rows, jac_cols])
        else:
            scale, offset = unit_conversion(input_units, units)
            self._conversion_factors[output_name] = scale, offset

            self.declare_partials(of=output_name, wrt=input_name,
                                  rows=jac_rows, cols=jac_cols,
                                  val=scale * jac[jac_rows, jac_cols])

        return added_source

    def compute(self, inputs, outputs):
        """
        Compute component outputs.

        Parameters
        ----------
        inputs : `Vector`
            `Vector` containing inputs.
        outputs : `Vector`
            `Vector` containing outputs.
        """
        # convert dt_dstau to a column vector
        dt_dstau = inputs['dt_dstau'][:, np.newaxis]

        for (input_name, output_name, _, is_rate) in self._vars.values():
            if self._no_interp:
                interp_vals = inputs[input_name]
            else:
                inp = inputs[input_name]
                if len(inp.shape) > 2:
                    # Dot product always performs the sum product over axis 2.
                    inp = inp.swapaxes(0, 1)
                interp_vals = self.interpolation_matrix.dot(inp)

            if is_rate:
                interp_vals = self.differentiation_matrix.dot(interp_vals) / dt_dstau

            if output_name in self._conversion_factors:
                scale, offset = self._conversion_factors[output_name]
                outputs[output_name] = scale * (interp_vals + offset)
            else:
                outputs[output_name] = interp_vals
