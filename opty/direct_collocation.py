#!/usr/bin/env python

from collections import OrderedDict

import numpy as np
import sympy as sym
from sympy.physics import mechanics as me
import ipopt

from .utils import ufuncify_matrix, parse_free, ObjectiveLambdaPrinter


class Problem(ipopt.problem):

    def __init__(self, objective, *args, **kwargs):
        """

        Parameters
        ----------
        objective : SymPy expression
            A scalar expression which is a function of the state, constants,
            and specifieds.
        bounds : dictionary
            This dictionary should contain a mapping from any of the
            symbolic states, unknown trajectories, or unknown parameters to
            a 2-tuple of floats, the first being the lower bound and the
            second the upper bound for that free variable, e.g. {x(t):
                (-1.0, 5.0)}.

        """

        self.bounds = kwargs.pop('bounds', None)

        self.collocator = ConstraintCollocator(*args, **kwargs)
        self.con = self.collocator.generate_constraint_function()
        self.con_jac = self.collocator.generate_jacobian_function()

        self.obj = Objective(objective,
                             self.collocator.state_symbols,
                             self.collocator.time_interval_symbol,
                             self.collocator.node_time_interval,
                             self.collocator.num_collocation_nodes,
                             self.collocator.unknown_input_trajectories,
                             self.collocator.unknown_parameters,
                             self.collocator.known_trajectory_map,
                             self.collocator.known_parameter_map)

        self.con_jac_rows, self.con_jac_cols = \
            self.collocator.jacobian_indices()

        self.num_free = self.collocator.num_free
        self.num_constraints = self.collocator.num_constraints

        self._generate_bound_arrays()

        # All constraints are expected to be equal to zero.
        con_bounds = np.zeros(self.num_constraints)

        super(Problem, self).__init__(n=self.num_free,
                                      m=self.num_constraints,
                                      lb=self.lower_bound,
                                      ub=self.upper_bound,
                                      cl=con_bounds,
                                      cu=con_bounds)

        self.obj_value = []

    def _generate_bound_arrays(self):
        INF = 10e19
        lb = -INF * np.ones(self.num_free)
        ub = INF * np.ones(self.num_free)

        N = self.collocator.num_collocation_nodes
        num_state_nodes = N * self.collocator.num_states
        num_non_par_nodes = N * (self.collocator.num_states +
                                 self.collocator.num_unknown_input_trajectories)
        state_syms = self.collocator.state_symbols
        unk_traj = self.collocator.unknown_input_trajectories
        unk_par = self.collocator.unknown_parameters

        if self.bounds is not None:
            for var, bounds in self.bounds.items():
                if var in state_syms:
                    i = state_syms.index(var)
                    start = i * N
                    stop = start + N
                    lb[start:stop] = bounds[0] * np.ones(N)
                    ub[start:stop] = bounds[1] * np.ones(N)
                elif var in unk_traj:
                    i = unk_traj.index(var)
                    start = num_state_nodes + i * N
                    stop = start + N
                    lb[start:stop] = bounds[0] * np.ones(N)
                    ub[start:stop] = bounds[1] * np.ones(N)
                elif var in unk_par:
                    i = unk_par.index(var)
                    idx = num_non_par_nodes + i
                    lb[idx] = bounds[0]
                    ub[idx] = bounds[1]

        self.lower_bound = lb
        self.upper_bound = ub

    def objective(self, free):
        return self.obj.evaluate(free)

    def gradient(self, free):
        # This should return a column vector.
        return self.obj.evaluate_gradient(free)

    def constraints(self, free):
        # This should return a column vector.
        return self.con(free)

    def jacobianstructure(self):
        return (self.con_jac_rows, self.con_jac_cols)

    def jacobian(self, free):
        return self.con_jac(free)

    def intermediate(self, *args):
        self.obj_value.append(args[2])


class Objective(object):

    def __init__(self, expr, states, interval_symbol, interval_value,
                 num_nodes, unknown_specifieds=tuple(),
                 unknown_constants=tuple(),
                 known_specifieds_map=OrderedDict(),
                 known_constants_map=OrderedDict()):
        """

        Parameters
        ==========
        expr : SymPy expression
            A scalar expression which is a function of the provided states,
            specifieds, and constants.
        states : tuple of SymPy functions of time
            The system states.
        interval_symbol : sympy.Symbol
            The symbol used for the node interval.
        interval_value : float
            The interval time in seconds.
        num_nodes : integer
            The number of nodes.
        unknown_specifieds : tuple of SymPy functions of time, optional
            The specified exongeous inputs.
        unknown_constants : tuple of SymPy symbols, optional
            The system constants.
        known_specifieds_map : collections.OrderedDict, optional
            A dictionary mapping SymPy functions of time to NumPy 1D arrays.
        known_constants_map : collections.OrderedDict, optional
            A dictionary mapping SymPy symbols to floats.

        """

        self.expr = expr
        self.states = states
        self.interval_symbol = interval_symbol
        self.interval_value = interval_value
        self.num_nodes = num_nodes

        self.unknown_specifieds = unknown_specifieds
        self.unknown_constants = unknown_constants
        self.known_specifieds_map = known_specifieds_map
        self.known_constants_map = known_constants_map

        self.free_symbols = states + unknown_specifieds + unknown_constants

        self._build_deferred_map()

        self._obj_grad_result = np.zeros(len(states) * num_nodes +
                                         len(unknown_specifieds) * num_nodes +
                                         len(unknown_constants))

        self._known_constants_values = np.array(
            self.known_constants_map.values())
        self._known_specifieds_values = np.empty((len(known_specifieds_map),
                                                  num_nodes),
                                                 dtype=float)

        for i, v in enumerate(self.known_specifieds_map.values()):
            self._known_specifieds_values[i] = v

        self._generate_base_obj()
        self._generate_base_obj_grad()

    def _build_deferred_map(self):

        vec_reps = ['x', 'r_u', 'p_u', 'r_k', 'p_k']
        sym_seqs = [self.states,
                    self.unknown_specifieds,
                    self.unknown_constants,
                    tuple(self.known_specifieds_map.keys()),
                    tuple(self.known_constants_map.keys())]

        self._deferred_vec_map = OrderedDict()

        for vec_sym, sym_seq in zip(vec_reps, sym_seqs):
            self._deferred_vec_map[sym.DeferredVector(vec_sym)] = sym_seq

        subs = {}
        for v, sym_seq in self._deferred_vec_map.items():
            for i, s in enumerate(sym_seq):
                subs[s] = v[i]

        self._deferred_sym_map = subs

    def _symbolic_partials(self):

        partials = OrderedDict()

        for s in self.free_symbols:
            partials[s] = self.expr.diff(s)

        return partials

    def _replace_with_deferred(self, expr):

        return expr.subs(self._deferred_sym_map)

    def _generate_base_obj(self):
        args = self._deferred_vec_map.keys()
        args.append(self.interval_symbol)

        f = sym.lambdify(args,
                         self._replace_with_deferred(self.expr),
                         printer=ObjectiveLambdaPrinter,
                         modules=({'Integral': np.sum,
                                   'ImmutableMatrix': np.array}, 'numpy'),
                         dummify=False)

        self._base_obj_func = f

    def _generate_base_obj_grad(self):

        args = self._deferred_vec_map.keys()
        args.append(self.interval_symbol)
        expr = sym.Matrix(self._symbolic_partials().values())

        f = sym.lambdify(args,
                         self._replace_with_deferred(expr),
                         printer=ObjectiveLambdaPrinter,
                         modules=({'Integral': np.sum,
                                   'ImmutableMatrix': np.array}, 'numpy'),
                         dummify=False)

        self._base_obj_grad_func = f

    def _eval_base_obj_grad(self, *args):

        partials = self._base_obj_grad_func(*args)

        for i, state in enumerate(self.states):
            start = i * self.num_nodes
            end = start + self.num_nodes
            self._obj_grad_result[start:end] = partials[i]

        base = len(self.states) * self.num_nodes
        for i, spec in enumerate(self.unknown_specifieds):
            start = base + i * self.num_nodes
            end = start + self.num_nodes
            self._obj_grad_result[start:end] = partials[len(self.states) + i]

        partial_base = len(self.states) + len(self.unknown_specifieds)
        base = partial_base * self.num_nodes
        for i, con in enumerate(self.unknown_constants):
            self._obj_grad_result[base + i] = partials[partial_base + i]

        return self._obj_grad_result

    def _generate_args(self, free):

        states, specifieds, constants = parse_free(free, len(self.states),
                                                   len(self.unknown_specifieds),
                                                   self.num_nodes)

        try:
            if len(specifieds.shape) < 2:
                specifieds = np.array([specifieds])
        except AttributeError:
            pass

        args = (states,
                specifieds,
                constants,
                self._known_specifieds_values,
                self._known_constants_values,
                self.interval_value)

        return args

    def evaluate(self, free):

        return self._base_obj_func(*self._generate_args(free))

    def evaluate_gradient(self, free):

        return self._eval_base_obj_grad(*self._generate_args(free))


class ConstraintCollocator(object):
    """This class is responsible for generating the constraint function and
    the sparse Jacobian of the constraint function using direct collocation
    methods for a non-linear programming problem where the essential
    constraints are defined from the equations of motion of the system.

    Notes
    -----
    N : number of collocation nodes
    N - 1 : number of constraints
    n : number of states
    m : number of input trajectories
    p : number of parameters
    q : number of unknown input trajectories
    r : number of unknown parameters

    """

    time_interval_symbol = sym.Symbol('h', real=True)

    def __init__(self, equations_of_motion, state_symbols,
                 num_collocation_nodes, node_time_interval,
                 known_parameter_map={}, known_trajectory_map={},
                 instance_constraints=None, time_symbol=None, tmp_dir=None,
                 integration_method='backward euler'):
        """
        Parameters
        ----------
        equations_of_motion : sympy.Matrix, shape(n, 1)
            A column matrix of SymPy expressions defining the right hand
            side of the equations of motion when the left hand side is zero,
            e.g. 0 = x'(t) - f(x(t), u(t), p) or 0 = f(x'(t), x(t), u(t),
            p). These should be in first order form but not necessairly
            explicit.
        state_symbols : iterable
            An iterable containing all of the SymPy functions of time which
            represent the states in the equations of motion.
        num_collocation_nodes : integer
            The number of collocation nodes, N. All known trajectory arrays
            should be of this length.
        node_time_interval : float
            The time interval between collocation nodes.
        known_parameter_map : dictionary, optional
            A dictionary that maps the SymPy symbols representing the known
            constant parameters to floats. Any parameters in the equations
            of motion not provided in this dictionary will become free
            optimization variables.
        known_trajectory_map : dictionary, optional
            A dictionary that maps the non-state SymPy functions of time to
            ndarrays of floats of shape(N,). Any time varying parameters in
            the equations of motion not provided in this dictionary will
            become free trajectories optimization variables.
        integration_method : string, optional
            The integration method to use, either `backward euler` or
            `midpoint`.
        instance_constraints : iterable of SymPy expressions
            These expressions are for constraints on the states at specific
            time points. They can be expressions with any state instance and
            any of the known parameters found in the equations of motion.
            All states should be evaluated at a specific instant of time.
            For example, the constraint x(0) = 5.0 would be specified as
            x(0) - 5.0 and the constraint x(0) = x(5.0) would be specified
            as  x(0) - x(5.0). Unknown parameters and time varying
            parameters other than the states are currently not supported.
        time_symbol : SymPy Symbol, optional
            The symbol representating time in the equations of motion. If
            not given, it is assumed to be the default stored in
            dynamicsymbols._t.
        tmp_dir : string, optional
            If you want to see the generated Cython and C code for the
            constraint and constraint Jacobian evaluations, pass in a path
            to a directory here.

        """
        self.eom = equations_of_motion

        if time_symbol is not None:
            self.time_symbol = time_symbol
        else:
            self.time_symbol = me.dynamicsymbols._t

        self.state_symbols = tuple(state_symbols)
        self.state_derivative_symbols = tuple([s.diff(self.time_symbol) for
                                               s in state_symbols])
        self.num_states = len(self.state_symbols)

        self.num_collocation_nodes = num_collocation_nodes
        self.node_time_interval = node_time_interval

        self.known_parameter_map = known_parameter_map
        self.known_trajectory_map = known_trajectory_map

        self.instance_constraints = instance_constraints

        self.num_constraints = self.num_states * (num_collocation_nodes - 1)

        self.tmp_dir = tmp_dir

        self._sort_parameters()
        self._check_known_trajectories()
        self._sort_trajectories()
        self.num_free = ((self.num_states +
                          self.num_unknown_input_trajectories) *
                         self.num_collocation_nodes +
                         self.num_unknown_parameters)

        self.integration_method = integration_method

        if instance_constraints is not None:
            self.num_instance_constraints = len(instance_constraints)
            self.num_constraints += self.num_instance_constraints
            self._identify_functions_in_instance_constraints()
            self._find_closest_free_index()
            self.eval_instance_constraints = \
                self._instance_constraints_func()
            self.eval_instance_constraints_jacobian_values = \
                self._instance_constraints_jacobian_values_func()

    @property
    def integration_method(self):
        return self._integration_method

    @integration_method.setter
    def integration_method(self, method):
        if method not in ['backward euler', 'midpoint']:
            msg = ("{} is not a valid integration method.")
            raise ValueError(msg.format(method))
        else:
            self._integration_method = method
            self._discrete_symbols()
            self._discretize_eom()

    @staticmethod
    def _parse_inputs(all_syms, known_syms):
        """Returns sets of symbols and their counts, based on if the known
        symbols exist in the set of all symbols.

        Parameters
        ----------
        all_syms : sequence
            A set of SymPy symbols or functions.
        known_syms : sequence
            A set of SymPy symbols or functions.

        Returns
        -------
        known : tuple
            The set of known symbols.
        num_known : integer
            The number of known symbols.
        unknown : tuple
            The set of unknown symbols in all_syms.
        num_unknown :integer
            The number of unknown symbols.

        """
        all_syms = set(all_syms)
        known_syms = list(known_syms)

        # Remove any symbols that are extraneous.
        for s in known_syms:
            if s not in all_syms:
                known_syms.remove(s)

        def sort_sympy(seq):
            seq = list(seq)
            try:  # symbols
                seq.sort(key=lambda x: x.name)
            except AttributeError:  # functions
                seq.sort(key=lambda x: x.__class__.__name__)
            return seq

        if not all_syms:  # if empty sequence
            known = tuple()
            num_known = 0
            unknown = tuple()
            num_unknown = 0
        else:
            if known_syms:
                known = tuple(known_syms)  # don't sort known syms
                num_known = len(known)
                unknown = tuple(sort_sympy(all_syms.difference(known)))
                num_unknown = len(unknown)
            else:
                known = tuple()
                num_known = 0
                unknown = tuple(sort_sympy(all_syms))
                num_unknown = len(unknown)

        return known, num_known, unknown, num_unknown

    def _sort_parameters(self):
        """Finds and counts all of the parameters in the equations of motion
        and categorizes them based on which parameters the user supplies.
        The unknown parameters are sorted by name."""

        parameters = self.eom.free_symbols.copy()
        parameters.remove(self.time_symbol)

        res = self._parse_inputs(parameters,
                                 self.known_parameter_map.keys())

        self.known_parameters = res[0]
        self.num_known_parameters = res[1]
        self.unknown_parameters = res[2]
        self.num_unknown_parameters = res[3]

        self.parameters = res[0] + res[2]
        self.num_parameters = len(self.parameters)

    def _check_known_trajectories(self):
        """Raises and error if the known tracjectories are not the correct
        length."""

        N = self.num_collocation_nodes

        for k, v in self.known_trajectory_map.items():
            if len(v) != N:
                msg = 'The known parameter {} is not length {}'
                raise ValueError(msg.format(k, N))

    def _sort_trajectories(self):
        """Finds and counts all of the non-state, time varying parameters in
        the equations of motion and categorizes them based on which
        parameters the user supplies. The unknown parameters are sorted by
        name. Any extraneous known parameters are ignored."""

        states = set(self.state_symbols)
        states_derivatives = set(self.state_derivative_symbols)
        state_related = states.union(states_derivatives)

        time_varying_symbols = me.find_dynamicsymbols(self.eom)

        non_states = time_varying_symbols.difference(state_related)

        res = self._parse_inputs(non_states,
                                 self.known_trajectory_map.keys())

        self.known_input_trajectories = res[0]
        self.num_known_input_trajectories = res[1]
        self.unknown_input_trajectories = res[2]
        self.num_unknown_input_trajectories = res[3]

        self.input_trajectories = res[0] + res[2]
        self.num_input_trajectories = len(self.input_trajectories)

    def _discrete_symbols(self):
        """Instantiates discrete symbols for each time varying variable in
        the euqations of motion.

        Instantiates
        ------------
        previous_discrete_state_symbols : tuple of sympy.Symbols
            The n symbols representing the system's (ith - 1) states.
        current_discrete_state_symbols : tuple of sympy.Symbols
            The n symbols representing the system's ith states.
        next_discrete_state_symbols : tuple of sympy.Symbols
            The n symbols representing the system's (ith + 1) states.
        current_known_discrete_specified_symbols : tuple of sympy.Symbols
            The symbols representing the system's ith known input
            trajectories.
        next_known_discrete_specified_symbols : tuple of sympy.Symbols
            The symbols representing the system's (ith + 1) known input
            trajectories.
        current_unknown_discrete_specified_symbols : tuple of sympy.Symbols
            The symbols representing the system's ith unknown input
            trajectories.
        next_unknown_discrete_specified_symbols : tuple of sympy.Symbols
            The symbols representing the system's (ith + 1) unknown input
            trajectories.
        current_discrete_specified_symbols : tuple of sympy.Symbols
            The m symbols representing the system's ith specified inputs.
        next_discrete_specified_symbols : tuple of sympy.Symbols
            The m symbols representing the system's (ith + 1) specified
            inputs.

        """

        # The previus, current, and next states.
        self.previous_discrete_state_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'p', real=True)
                   for f in self.state_symbols])
        self.current_discrete_state_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'i', real=True)
                   for f in self.state_symbols])
        self.next_discrete_state_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'n', real=True)
                   for f in self.state_symbols])

        # The current and next known input trajectories.
        self.current_known_discrete_specified_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'i', real=True)
                   for f in self.known_input_trajectories])
        self.next_known_discrete_specified_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'n', real=True)
                   for f in self.known_input_trajectories])


        # The current and next unknown input trajectories.
        self.current_unknown_discrete_specified_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'i', real=True)
                   for f in self.unknown_input_trajectories])
        self.next_unknown_discrete_specified_symbols = \
            tuple([sym.Symbol(f.__class__.__name__ + 'n', real=True)
                   for f in self.unknown_input_trajectories])

        self.current_discrete_specified_symbols = \
            (self.current_known_discrete_specified_symbols +
             self.current_unknown_discrete_specified_symbols)
        self.next_discrete_specified_symbols = \
            (self.next_known_discrete_specified_symbols +
             self.next_unknown_discrete_specified_symbols)

    def _discretize_eom(self):
        """Instantiates the constraint equations in a discretized form using
        backward Euler discretization.

        Instantiates
        ------------
        discrete_eoms : sympy.Matrix, shape(n, 1)
            The column vector of the discretized equations of motion.

        """
        x = self.state_symbols
        xd = self.state_derivative_symbols
        u = self.input_trajectories

        xp = self.previous_discrete_state_symbols
        xi = self.current_discrete_state_symbols
        xn = self.next_discrete_state_symbols
        ui = self.current_discrete_specified_symbols
        un = self.next_discrete_specified_symbols

        h = self.time_interval_symbol

        if self.integration_method == 'backward euler':

            deriv_sub = {d: (i - p) / h for d, i, p in zip(xd, xi, xp)}

            func_sub = dict(zip(x + u, xi + ui))

            self.discrete_eom = me.msubs(self.eom, deriv_sub, func_sub)

        elif self.integration_method == 'midpoint':

            xdot_sub = {d: (n - i) / h for d, i, n in zip(xd, xi, xn)}
            x_sub = {d: (i + n) / 2 for d, i, n in zip(x, xi, xn)}
            u_sub = {d: (i + n) / 2 for d, i, n in zip(u, ui, un)}
            self.discrete_eom = me.msubs(self.eom, xdot_sub, x_sub, u_sub)

    def _identify_functions_in_instance_constraints(self):
        """Instantiates a set containing all of the instance functions, i.e.
        x(1.0) in the instance constraints."""

        all_funcs = set()

        for con in self.instance_constraints:
            all_funcs = all_funcs.union(con.atoms(sym.Function))

        self.instance_constraint_function_atoms = all_funcs

    def _find_closest_free_index(self):
        """Instantiates a dictionary mapping the instance functions to the
        nearest index in the free variables vector."""

        def determine_free_index(time_index, state):
            state_index = self.state_symbols.index(state)
            return time_index + state_index * self.num_collocation_nodes

        N = self.num_collocation_nodes
        h = self.node_time_interval
        duration = h * (N - 1)

        time_vector = np.linspace(0.0, duration, num=N)

        node_map = {}
        for func in self.instance_constraint_function_atoms:
            time_value = func.args[0]
            time_index = np.argmin(np.abs(time_vector - time_value))
            free_index = determine_free_index(time_index,
                                              func.__class__(self.time_symbol))
            node_map[func] = free_index

        self.instance_constraints_free_index_map = node_map

    def _instance_constraints_func(self):
        """Returns a function that evaluates the instance constraints given
        the free optimization variables."""
        free = sym.DeferredVector('FREE')
        def_map = {k: free[v] for k, v in
                   self.instance_constraints_free_index_map.items()}
        subbed_constraints = [con.subs(def_map) for con in
                              self.instance_constraints]
        f = sym.lambdify(([free] + self.known_parameter_map.keys()),
                         subbed_constraints, modules=[{'ImmutableMatrix':
                                                       np.array}, "numpy"])
        def wrapped(free):
            return f(free, *self.known_parameter_map.values())

        return wrapped

    def _instance_constraints_jacobian_indices(self):
        """Returns the row and column indices of the non-zero values in the
        Jacobian of the constraints."""
        idx_map = self.instance_constraints_free_index_map

        num_eom_constraints = 2 * (self.num_collocation_nodes - 1)

        rows = []
        cols = []

        for i, con in enumerate(self.instance_constraints):
            funcs = con.atoms(sym.Function)
            indices = [idx_map[f] for f in funcs]
            row_idxs = num_eom_constraints + i * np.ones(len(indices),
                                                         dtype=int)
            rows += list(row_idxs)
            cols += indices

        return np.array(rows), np.array(cols)

    def _instance_constraints_jacobian_values_func(self):
        """Retruns the non-zero values of the constraint Jacobian associated
        with the instance constraints."""
        free = sym.DeferredVector('FREE')

        def_map = {k: free[v] for k, v in
                   self.instance_constraints_free_index_map.items()}

        funcs = []
        num_vals_per_func = []
        for con in self.instance_constraints:
            partials = list(con.atoms(sym.Function))
            num_vals_per_func.append(len(partials))
            jac = sym.Matrix([con]).jacobian(partials)
            jac = jac.subs(def_map)
            funcs.append(sym.lambdify(([free] +
                                       self.known_parameter_map.keys()),
                                      jac,
                                      modules=[{'ImmutableMatrix': np.array},
                                               "numpy"]))
        l = np.sum(num_vals_per_func)

        def wrapped(free):
            arr = np.zeros(l)
            j = 0
            for i, (f, num) in enumerate(zip(funcs, num_vals_per_func)):
                arr[j:j + num] = f(free, *self.known_parameter_map.values())
                j += num
            return arr

        return wrapped

    def _gen_multi_arg_con_func(self):
        """Instantiates a function that evaluates the constraints given all
        of the arguments of the functions, i.e. not just the free
        optimization variables.

        Instantiates
        ------------
        _multi_arg_con_func : function
            A function which returns the numerical values of the constraints
            at collocation nodes 2,...,N.

        Notes
        -----
        args:
            all current states (x1i, ..., xni)
            all previous states (x1p, ... xnp)
            all current specifieds (s1i, ..., smi)
            parameters (c1, ..., cb)
            time interval (h)

            args: (x1i, ..., xni, x1p, ... xnp, s1i, ..., smi, c1, ..., cb, h)
            n: num states
            m: num specified
            b: num parameters

        The function should evaluate and return an array:

            [con_1_2, ..., con_1_N, con_2_2, ...,
             con_2_N, ..., con_n_2, ..., con_n_N]

        for n states and N-1 constraints at the time points.

        """
        xi_syms = self.current_discrete_state_symbols
        xp_syms = self.previous_discrete_state_symbols
        xn_syms = self.next_discrete_state_symbols
        si_syms = self.current_discrete_specified_symbols
        sn_syms = self.next_discrete_specified_symbols
        h_sym = self.time_interval_symbol
        constant_syms = self.known_parameters + self.unknown_parameters

        if self.integration_method == 'backward euler':

            args = [x for x in xi_syms] + [x for x in xp_syms]
            args += [s for s in si_syms] + list(constant_syms) + [h_sym]

            current_start = 1
            current_stop = None
            adjacent_start = None
            adjacent_stop = -1

        elif self.integration_method == 'midpoint':

            args = [x for x in xi_syms] + [x for x in xn_syms]
            args += [s for s in si_syms] + [s for s in sn_syms]
            args += list(constant_syms) + [h_sym]

            current_start = None
            current_stop = -1
            adjacent_start = 1
            adjacent_stop = None

        f = ufuncify_matrix(args, self.discrete_eom,
                            const=constant_syms + (h_sym,),
                            tmp_dir=self.tmp_dir)

        def constraints(state_values, specified_values, constant_values,
                        interval_value):
            """Returns a vector of constraint values given all of the
            unknowns in the equations of motion over the 2, ..., N time
            steps.

            Parameters
            ----------
            states : ndarray, shape(n, N)
                The array of n states through N time steps.
            specified_values : ndarray, shape(m, N) or shape(N,)
                The array of m specifieds through N time steps.
            constant_values : ndarray, shape(b,)
                The array of b parameters.
            interval_value : float
                The value of the discretization time interval.

            Returns
            -------
            constraints : ndarray, shape(N-1,)
                The array of constraints from t = 2, ..., N.
                [con_1_2, ..., con_1_N, con_2_2, ...,
                 con_2_N, ..., con_n_2, ..., con_n_N]

            """

            if state_values.shape[0] < 2:
                raise ValueError('There should always be at least two states.')

            assert state_values.shape == (self.num_states,
                                          self.num_collocation_nodes)

            x_current = state_values[:, current_start:current_stop]  # n x N - 1
            x_adjacent = state_values[:, adjacent_start:adjacent_stop]  # n x N - 1

            # 2n x N - 1
            args = [x for x in x_current] + [x for x in x_adjacent]

            # 2n + m x N - 1
            if len(specified_values.shape) == 2:
                assert specified_values.shape == \
                    (self.num_input_trajectories,
                     self.num_collocation_nodes)
                si = specified_values[:, current_start:current_stop]
                args += [s for s in si]
                if self.integration_method == 'midpoint':
                    sn = specified_values[:, adjacent_start:adjacent_stop]
                    args += [s for s in sn]
            elif len(specified_values.shape) == 1 and specified_values.size != 0:
                assert specified_values.shape == \
                    (self.num_collocation_nodes,)
                si = specified_values[current_start:current_stop]
                args += [si]
                if self.integration_method == 'midpoint':
                    sn = specified_values[adjacent_start:adjacent_stop]
                    args += [sn]

            args += [c for c in constant_values]
            args += [interval_value]

            num_constraints = state_values.shape[1] - 1

            # TODO : Move this to an attribute of the class so that it is
            # only initialized once and just reuse it on each evaluation of
            # this function.
            result = np.empty((num_constraints, state_values.shape[0]))

            return f(result, *args).T.flatten()

        self._multi_arg_con_func = constraints

    def jacobian_indices(self):
        """Returns the row and column indices for the non-zero values in the
        constraint Jacobian.

        Returns
        -------
        jac_row_idxs : ndarray, shape(2 * n + q + r,)
            The row indices for the non-zero values in the Jacobian.
        jac_col_idxs : ndarray, shape(n,)
            The column indices for the non-zero values in the Jacobian.

        """

        N = self.num_collocation_nodes
        n = self.num_states

        num_constraint_nodes = N - 1

        if self.integration_method == 'backward euler':

            num_partials = n * (2 * n + self.num_unknown_input_trajectories
                                + self.num_unknown_parameters)

        elif self.integration_method == 'midpoint':

            num_partials = n * (2 * n + 2 *
                                self.num_unknown_input_trajectories +
                                self.num_unknown_parameters)

        num_non_zero_values = num_constraint_nodes * num_partials

        if self.instance_constraints is not None:
            ins_row_idxs, ins_col_idxs = \
                self._instance_constraints_jacobian_indices()
            num_non_zero_values += len(ins_row_idxs)

        jac_row_idxs = np.empty(num_non_zero_values, dtype=int)
        jac_col_idxs = np.empty(num_non_zero_values, dtype=int)

        """
        The symbolic derivative matrix for a single constraint node follows
        these patterns:

        Backward Euler
        --------------
        i: ith, p: ith-1

        For example:
        x1i = the first state at the ith constraint node
        uqi = the qth state at the ith constraint node
        uqn = the qth state at the ith+1 constraint node

        [x1] [x1i, ..., xni, x1p, ..., xnp, u1i, .., uqi, p1, ..., pr]
        [. ]
        [. ]
        [. ]
        [xn]

        Midpoint
        --------
        i: ith, n: ith+1

        [x1] [x1i, ..., xni, x1n, ..., xnn, u1i, .., uqi, u1n, ..., uqn, p1, ..., pp]
        [. ]
        [. ]
        [. ]
        [xn]

        Each of these matrices are evaulated at N-1 constraint nodes and
        then the 3D matrix is flattened into a 1d array. The backward euler
        uses nodes 1 <= i <= N-1 and the midpoint uses 0 <= i <= N - 2. So
        the flattened arrays looks like:

        M = N-1
        P = N-2

        Backward Euler
        --------------

        i=1  x1  | [x11, ..., xn1, x10, ..., xn0, u11, .., uq1, p1, ..., pr,
             x2  |  x11, ..., xn1, x10, ..., xn0, u11, .., uq1, p1, ..., pr,
             ... |  ...,
             xn  |  x11, ..., xn1, x10, ..., xn0, u11, .., uq1, p1, ..., pr,
        i=2  x1  |  x12, ..., xn2, x11, ..., xn1, u12, .., uq2, p1, ..., pr,
             x2  |  x12, ..., xn2, x11, ..., xn1, u12, .., uq2, p1, ..., pr,
             ... |  ...,
             xn  |  x12, ..., xn2, x11, ..., xn1, u12, .., uq2, p1, ..., pr,
                 |  ...,
        i=M  x1  |  x1M, ..., xnM, x1P, ..., xnP, u1M, .., uqM, p1, ..., pr,
             x2  |  x1M, ..., xnM, x1P, ..., xnP, u1M, .., uqM, p1, ..., pr,
             ... |  ...,
             xn  |  x1M, ..., xnM, x1P, ..., xnP, u1M, .., uqM, p1, ..., pr]

        Midpoint
        --------

        i=0   x1  | [x10, ..., xn0, x11, ..., xn1, u10, .., uq0, u11, .., uq1, p1, ..., pr,
                x2  |  x10, ..., xn0, x11, ..., xn1, u10, .., uq0, u11, .., uq1, p1, ..., pr,
                ... |  ...,
                xn  |  x10, ..., xn0, x11, ..., xn1, u10, .., uq0, u11, .., uq1, p1, ..., pr,
        i=1   x1  |  x11, ..., xn1, x12, ..., xn2, u11, .., uq1, u12, .., uq2, p1, ..., pr,
                x2  |  x11, ..., xn1, x12, ..., xn2, u11, .., uq1, u12, .., uq2, p1, ..., pr,
                ... |  ...,
                xn  |  x11, ..., xn1, x12, ..., xn2, u11, .., uq1, u12, .., uq2, p1, ..., pr,
                ... |  ...,
        i=P   x1  |  x1P, ..., xnP, x1M, ..., xnM, u1P, .., uqP, u1M, .., uqM, p1, ..., pr,
                x2  |  x1P, ..., xnP, x1M, ..., xnM, u1P, .., uqP, u1M, .., uqM, p1, ..., pr,
                ... |  ...,
                xn  |  x1P, ..., xnP, x1M, ..., xnM, u1P, .., uqP, u1M, .., uqM, p1, ..., pr]

        These two arrays contain of the non-zero values of the sparse
        Jacobian[#]_.

        .. [#] Some of the partials can be equal to zero and could be
            excluded from the array. These could be a significant number.

        Now we need to generate the triplet format indices of the full
        sparse Jacobian for each one of the entries in these arrays. The
        format of the Jacobian matrix is:

        Backward Euler
        --------------

                [x10, ..., x1N-1, ..., xn0, ..., xnN-1, u10, ..., u1N-1, ..., uq0, ..., uqN-1, p1, ..., pr]
        [x11]
        [x12]
        [...]
        [x1M]
        [...]
        [xn1]
        [xn2]
        [...]
        [xnM]

        Midpoint
        --------

                [x10, ..., x1N-1, ..., xn0, ..., xnN-1, u10, ..., u1N-1, ..., uq0, ..., uqN-1, p1, ..., pr]
        [x10]
        [x11]
        [...]
        [x1P]
        [...]
        [xn0]
        [xn1]
        [...]
        [xnP]


        """
        for i in range(num_constraint_nodes):

            # n : number of states
            # m : number of input trajectories
            # p : number of parameters
            # q : number of unknown input trajectories
            # r : number of unknown parameters

            # the states repeat every N - 1 constraints
            # row_idxs = [0 * (N - 1), 1 * (N - 1),  2 * (N - 1), ..., n * (N - 1)]

            # This gives the Jacobian row indices matching the ith
            # constraint node for each state. ith corresponds to the loop
            # indice.
            row_idxs = [j * (num_constraint_nodes) + i for j in range(n)]

            # first row, the columns indices mapping is:
            # [1, N + 1, ..., N - 1] : [x1p, x1i, 0, ..., 0]
            # [0, N, ..., 2 * (N - 1)] : [x2p, x2i, 0, ..., 0]
            # [-p:] : p1,..., pp  the free constants

            # i=0: [1, ..., n * N + 1, 0, ..., n * N + 0, n * N:n * N + p]
            # i=1: [2, ..., n * N + 2, 1, ..., n * N + 1, n * N:n * N + p]
            # i=2: [3, ..., n * N + 3, 2, ..., n * N + 2, n * N:n * N + p]

            if self.integration_method == 'backward euler':

                col_idxs = [j * N + i + 1 for j in range(n)]
                col_idxs += [j * N + i for j in range(n)]
                col_idxs += [n * N + j * N + i + 1 for j in
                            range(self.num_unknown_input_trajectories)]
                col_idxs += [(n + self.num_unknown_input_trajectories) * N + j
                            for j in range(self.num_unknown_parameters)]

            elif self.integration_method == 'midpoint':

                col_idxs = [j * N + i for j in range(n)]
                col_idxs += [j * N + i + 1 for j in range(n)]
                col_idxs += [n * N + j * N + i for j in
                            range(self.num_unknown_input_trajectories)]
                col_idxs += [n * N + j * N + i + 1 for j in
                            range(self.num_unknown_input_trajectories)]
                col_idxs += [(n + self.num_unknown_input_trajectories) * N + j
                            for j in range(self.num_unknown_parameters)]

            row_idx_permutations = np.repeat(row_idxs, len(col_idxs))
            col_idx_permutations = np.array(list(col_idxs) * len(row_idxs),
                                            dtype=int)

            start = i * num_partials
            stop = (i + 1) * num_partials
            jac_row_idxs[start:stop] = row_idx_permutations
            jac_col_idxs[start:stop] = col_idx_permutations

        if self.instance_constraints is not None:
            jac_row_idxs[-len(ins_row_idxs):] = ins_row_idxs
            jac_col_idxs[-len(ins_col_idxs):] = ins_col_idxs

        return jac_row_idxs, jac_col_idxs

    def _gen_multi_arg_con_jac_func(self):
        """Instantiates a function that evaluates the Jacobian of the
        constraints.

        Instantiates
        ------------
        _multi_arg_con_jac_func : function
            A function which returns the numerical values of the constraints
            at time points 2,...,N.

        """
        xi_syms = self.current_discrete_state_symbols
        xp_syms = self.previous_discrete_state_symbols
        xn_syms = self.next_discrete_state_symbols
        si_syms = self.current_discrete_specified_symbols
        sn_syms = self.next_discrete_specified_symbols
        ui_syms = self.current_unknown_discrete_specified_symbols
        un_syms = self.next_unknown_discrete_specified_symbols
        h_sym = self.time_interval_symbol
        constant_syms = self.known_parameters + self.unknown_parameters

        if self.integration_method == 'backward euler':

            # The free parameters are always the n * (N - 1) state values,
            # the unknown input trajectories, and the unknown model
            # constants, so the base Jacobian needs to be taken with respect
            # to the ith, and ith - 1 states, and the free model constants.
            wrt = (xi_syms + xp_syms + ui_syms + self.unknown_parameters)

            # The arguments to the Jacobian function include all of the free
            # Symbols/Functions in the matrix expression.
            args = xi_syms + xp_syms + si_syms + constant_syms + (h_sym,)

            current_start = 1
            current_stop = None
            adjacent_start = None
            adjacent_stop = -1

        elif self.integration_method == 'midpoint':

            wrt = (xi_syms + xn_syms + ui_syms + un_syms +
                   self.unknown_parameters)

            # The arguments to the Jacobian function include all of the free
            # Symbols/Functions in the matrix expression.
            args = (xi_syms + xn_syms + si_syms + sn_syms + constant_syms +
                    (h_sym,))

            current_start = None
            current_stop = -1
            adjacent_start = 1
            adjacent_stop = None

        # This creates a matrix with all of the symbolic partial derivatives
        # necessary to compute the full Jacobian.
        symbolic_partials = self.discrete_eom.jacobian(wrt)

        # This generates a numerical function that evaluates the matrix of
        # partial derivatives. This function returns the non-zero elements
        # needed to build the sparse constraint Jacobian.
        eval_partials = ufuncify_matrix(args, symbolic_partials,
                                        const=constant_syms + (h_sym,),
                                        tmp_dir=self.tmp_dir)

        result = np.empty((self.num_collocation_nodes - 1,
                           symbolic_partials.shape[0] *
                           symbolic_partials.shape[1]))

        def constraints_jacobian(state_values, specified_values,
                                 parameter_values, interval_value):
            """Returns the values of the sparse constraing Jacobian matrix
            given all of the values for each variable in the equations of
            motion over the N - 1 nodes.

            Parameters
            ----------
            states : ndarray, shape(n, N)
                The array of n states through N time steps. There are always
                at least two states.
            specified_values : ndarray, shape(m, N) or shape(N,)
                The array of m specified inputs through N time steps.
            parameter_values : ndarray, shape(p,)
                The array of p parameter.
            interval_value : float
                The value of the discretization time interval.

            Returns
            -------
            constraint_jacobian_values : ndarray, shape(see below,)
                backward euler: shape((N - 1) * n * (2*n + q + r),)
                midpoint: shape((N - 1) * n * (2*n + 2*q + r),)
                The values of the non-zero entries of the constraints
                Jacobian. These correspond to the triplet formatted indices
                returned from jacobian_indices.

            Notes
            -----
            N : number of collocation nodes
            n : number of states
            m : number of input trajectories
            p : number of parameters
            q : number of unknown input trajectories
            r : number of unknown parameters
            n * (N - 1) : number of constraints

            """
            if state_values.shape[0] < 2:
                raise ValueError('There should always be at least two states.')

            # Each of these arrays are shape(n, N - 1). The x_adjacent is
            # either the previous value of the state or the next value of
            # the state, depending on the integration method.
            x_current = state_values[:, current_start:current_stop]
            x_adjacent = state_values[:, adjacent_start:adjacent_stop]

            # 2n x N - 1
            args = [x for x in x_current] + [x for x in x_adjacent]

            # 2n + m x N - 1
            if len(specified_values.shape) == 2:
                si = specified_values[:, current_start:current_stop]
                args += [s for s in si]
                if self.integration_method == 'midpoint':
                    sn = specified_values[:, adjacent_start:adjacent_stop]
                    args += [s for s in sn]
            elif len(specified_values.shape) == 1 and specified_values.size != 0:
                si = specified_values[current_start:current_stop]
                args += [si]
                if self.integration_method == 'midpoint':
                    sn = specified_values[adjacent_start:adjacent_stop]
                    args += [sn]

            args += [c for c in parameter_values]
            args += [interval_value]

            # backward euler: shape(N - 1, n, 2*n + q + r)
            # midpoint: shape(N - 1, n, 2*n + 2*q + r)
            non_zero_derivatives = eval_partials(result, *args)

            return non_zero_derivatives.ravel()

        self._multi_arg_con_jac_func = constraints_jacobian

    @staticmethod
    def _merge_fixed_free(syms, fixed, free, typ):
        """Returns an array with the fixed and free values combined. This
        just takes the known and unknown values and combines them for the
        function evaluation.

        This assumes that you have the free constants in the correct order.

        Parameters
        ----------
        syms : iterable of SymPy Symbols or Functions
        fixed : dictionary
            A mapping from Symbols to floats or Functions to 1d ndarrays.
        free : ndarray, (N,) or shape(n,N)
            An array
        typ : string
            traj or par


        """

        merged = []
        n = 0
        # syms is order as known (fixed) then unknown (free)
        for i, s in enumerate(syms):
            if s in fixed.keys():
                merged.append(fixed[s])
            else:
                if typ == 'traj' and len(free.shape) == 1:
                    merged.append(free)
                else:
                    merged.append(free[n])
                    n += 1
        return np.array(merged)

    def _wrap_constraint_funcs(self, func, typ):
        """Returns a function that evaluates all of the constraints or
        Jacobian of the constraints given the free optimization variables.

        Parameters
        ----------
        func : function
            A function that takes the full parameter set and evaluates the
            constraint functions or the Jacobian of the contraint functions.
            i.e. the output of _gen_multi_arg_con_func or
            _gen_multi_arg_con_jac_func.

        Returns
        -------
        func : function
            A function which returns constraint values given the system's
            free optimization variables.

        """

        def constraints(free):

            free_states, free_specified, free_constants = \
                parse_free(free, self.num_states,
                           self.num_unknown_input_trajectories,
                           self.num_collocation_nodes)

            all_specified = self._merge_fixed_free(self.input_trajectories,
                                                   self.known_trajectory_map,
                                                   free_specified, 'traj')

            all_constants = self._merge_fixed_free(self.parameters,
                                                   self.known_parameter_map,
                                                   free_constants, 'par')

            eom_con_vals = func(free_states, all_specified, all_constants,
                                self.node_time_interval)

            if self.instance_constraints is not None:
                if typ == 'con':
                    ins_con_vals = self.eval_instance_constraints(free)
                elif typ == 'jac':
                    ins_con_vals = self.eval_instance_constraints_jacobian_values(free)
                return np.hstack((eom_con_vals, ins_con_vals))
            else:
                return eom_con_vals

        intro, second = func.__doc__.split('Parameters')
        params, returns = second.split('Returns')
        new_doc = '{}Parameters\n----------\nfree : ndarray, shape()\n\nReturns\n{}'
        constraints.__doc__ = new_doc.format(intro, returns)

        return constraints

    def generate_constraint_function(self):
        """Returns a function which evaluates the constraints given the
        array of free optimization variables."""
        self._gen_multi_arg_con_func()
        return self._wrap_constraint_funcs(self._multi_arg_con_func, 'con')

    def generate_jacobian_function(self):
        """Returns a function which evaluates the Jacobian of the
        constraints given the array of free optimization variables."""
        self._gen_multi_arg_con_jac_func()
        return self._wrap_constraint_funcs(self._multi_arg_con_jac_func, 'jac')
