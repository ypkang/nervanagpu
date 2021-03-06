# Copyright 2014 Nervana Systems Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import numpy as np
from pycuda.compiler import SourceModule
from pycuda.tools import context_dependent_memoize
from pytools import memoize_method
import pycuda.driver as drv
import nervanagpu as ng
# from ipdb import set_trace

_ew_template = r"""

#include <float.h>

%(common)s

#define THREADS %(threads)s

__global__ void %(name)s (
    unsigned* rand_state,
    %(arguments)s)
{
    const int tid  = threadIdx.x;
    const int bid  = blockIdx.x;

    extern __shared__ float sPartials[];

    %(inits)s
"""

_stage_template = {
    "loop" : r"""

    for (int i = tid; i < n{0}; i += THREADS)
    {{
        %(loads{0})s

        %(ops{0})s
    }}
""",

    "red32" :r"""

    #pragma unroll
    for (int i = 16; i > 0; i >>= 1)
    {{
        %(shfl_red{0})s
    }}

""",

    "red" :r"""

    sPartials[tid] = %(var_red{0})s;
    __syncthreads();

    #pragma unroll
    for (int a = THREADS >> 1; a > 32; a >>= 1)
    {{
        if ( tid < a )
            %(share1_red{0})s
        __syncthreads();
    }}

    if ( tid < 32 )
    {{
        %(share2_red{0})s

        #pragma unroll
        for (int i = 16; i > 0; i >>= 1)
            %(shfl_red{0})s

        sPartials[tid] = %(var_red{0})s;
    }}
    __syncthreads();
    %(var_red{0})s = sPartials[0];
""",

    "red_ops" :r"""

    %(ops{0})s
""",

    "red_out" : r"""

    if ( tid == 0 )
    {{
        %(ops{0})s
    }}
"""
}

_fin_template = r"""
    %(finish)s
}
"""

_init_rand_func = r"""
    unsigned lfsr0, lfsr1, lfsr2;
    unsigned idx = bid * THREADS + tid;
    rand_state += idx % (2048*32);
    lfsr0 = *rand_state;
    asm("mov.b32 %0, %%clock;"          : "=r"(lfsr1) :);
    asm("mov.b32 %0, %%globaltimer_lo;" : "=r"(lfsr2) :);
    asm("shf.r.clamp.b32 %0,%0,%0,%1;"  : "=r"(lfsr1) : "r"((lfsr1 & 31)^tid));
    asm("shf.r.clamp.b32 %0,%0,%0,%1;"  : "=r"(lfsr2) : "r"((lfsr2 & 31)^tid));
    lfsr1 ^= (idx << 3)  ^ (idx << 7);
    lfsr2 ^= (idx << 17) ^ (idx << 23);
"""

_init_rand_round_func = r"""
    int i_rand_scale = (127 - 32 - mantissa_bits) << 23;
    float rand_scale = *(float*)&i_rand_scale;
    unsigned rand_mask = 0xffffffff << (23 - mantissa_bits);
"""

_finish_rand_func = r"""
    *rand_state = lfsr0 ^ lfsr1 ^ lfsr2;
"""

_common_urand_gen = r"""
__device__ unsigned urand_gen(unsigned& lfsr0, unsigned& lfsr1, unsigned& lfsr2)
{
    lfsr0 = ((lfsr0 & 0xfffffffe) << 12) ^ (((lfsr0 << 13) ^ lfsr0) >> 19);
    lfsr1 = ((lfsr1 & 0xfffffff8) <<  4) ^ (((lfsr1 << 2)  ^ lfsr1) >> 25);
    lfsr2 = ((lfsr2 & 0xfffffff0) << 11) ^ (((lfsr2 << 3)  ^ lfsr2) >> 11);
    return lfsr0 ^ lfsr1 ^ lfsr2;
}
"""

_common_frand = r"""
__device__ __forceinline__ float frand(unsigned& lfsr0, unsigned& lfsr1, unsigned& lfsr2)
{
    unsigned urand = urand_gen(lfsr0, lfsr1, lfsr2);
    float val;
    asm("cvt.rn.f32.u32 %0, %1;\n\t"
        "mul.f32 %0, %0, 0F2f800000;"
        : "=f"(val) : "r"(urand));
    return val;
}
"""

_common_round = {

    "random" : {

        "f4" : r"""
__device__ float fp32_to_fp32_rand(
    float val, unsigned& lfsr0, unsigned& lfsr1, unsigned& lfsr2, float rand_scale, unsigned rand_mask)
{
    unsigned urand = urand_gen(lfsr0, lfsr1, lfsr2);

    float ret;
    asm("{\n\t"
        ".reg .f32 exponent, frand, result;\n\t"
        "and.b32 exponent, %1, 0xff800000;\n\t"
        "mul.f32 exponent, exponent, %2;\n\t"
        "cvt.rz.f32.u32 frand, %3;\n\t"
        "fma.rz.f32 result, exponent, frand, %1;\n\t"
        "and.b32 %0, result, %4;\n\t"
        "}" : "=f"(ret) : "f"(val), "f"(rand_scale), "r"(urand), "r"(rand_mask));

    return ret;
}
""",
        "f2" : r"""
__device__ unsigned short fp32_to_fp16_rand(
    float val, unsigned& lfsr0, unsigned& lfsr1, unsigned& lfsr2, float rand_scale, unsigned rand_mask)
{
    unsigned urand = urand_gen(lfsr0, lfsr1, lfsr2);

    unsigned short half;
    asm("{\n\t"
        ".reg .f16 result16;\n\t"
        ".reg .f32 exponent, frand, result32;\n\t"
        "and.b32 exponent, %1, 0xff800000;\n\t"
        "mul.f32 exponent, exponent, %2;\n\t"
        "cvt.rz.f32.u32 frand, %3;\n\t"
        "fma.rz.f32 result32, exponent, frand, %1;\n\t"
        "and.b32 result32, result32, %4;\n\t"
        "cvt.rz.f16.f32 result16, result32;\n\t"
        "mov.b16 %0, result16;\n\t"
        "}" : "=h"(half) : "f"(val), "f"(rand_scale), "r"(urand), "r"(rand_mask));

    return half;
}
""",
    },
    "nearest" : {

        "f2" : r"""
__device__ __forceinline__ unsigned short fp32_to_fp16(float val)
{
    unsigned short ret;
    asm("{\n\t"
        ".reg .f16 f16;\n\t"
        "cvt.rn.f16.f32 f16, %1;"
        "mov.b16 %0, f16;\n\t"
        "}" : "=h"(ret) : "f"(val));
    return ret;
}
""",
        "i1" : r"""
__device__ __forceinline__ char fp32_to_int8(float val)
{
    int ret;
    asm("cvt.rni.s8.f32 %0, %1;" : "=r"(ret) : "f"(val));
    return ret;
}
""",
        "u1" : r"""
__device__ __forceinline__ unsigned char fp32_to_uint8(float val)
{
    unsigned ret;
    asm("cvt.rni.u8.f32 %0, %1;" : "=r"(ret) : "f"(val));
    return ret;
}
""",
    },
}
# random rounding not used for these types
_common_round["random"]["i1"] = _common_round["nearest"]["i1"]
_common_round["random"]["u1"] = _common_round["nearest"]["u1"]


_common_fp16_to_fp32 = r"""
__device__ __forceinline__ float fp16_to_fp32(unsigned short val)
{
    float ret;
    asm("{\n\t"
        ".reg .f16 f16;\n\t"
        "mov.b16 f16, %1;\n\t"
        "cvt.f32.f16 %0, f16;"
        "}" : "=f"(ret) : "h"(val));
    return ret;
}
"""

_ew_types = {
    "f4" : {
        "type" : "float",
        "cvt"  : "",
    },
    "f2" : {
        "type" : "unsigned short",
        "cvt"  : "fp16_to_fp32",
    },
    "i1" : {
        "type" : "char",
        "cvt"  : "(float)",
    },
    "u1" : {
        "type" : "unsigned char",
        "cvt"  : "(float)",
    },
}

_ew_strings = {

    # 0: arg_id, 1: stage, 2: type, 3: cvt
    "in" : {
        "arguments" : "const {2}* a{0}_in, int row_strd{0}, int col_strd{0}",
        "inits"     : "const {2}* a{0}_in{1} = a{0}_in + bid * row_strd{0} + tid * col_strd{0};\n"
                  "    int a{0}_inc{1} = THREADS * col_strd{0};",
        "loads"     : "float a{0} = {3}(__ldg(a{0}_in{1}));\n"
              "        a{0}_in{1} += a{0}_inc{1};",
    },
    "out" : {
        "arguments" : "{2}* a_out, int row_strd, int col_strd",
        "inits"     : "a_out += bid * row_strd + tid * col_strd;\n"
                  "    int out_inc = THREADS * col_strd;",
    },
    "const" : {
        "arguments" : "float c{0}",
    },
    "round" : {
        "random"  : {
            "f4"  : "float {0}          = fp32_to_fp32_rand({1}, lfsr0, lfsr1, lfsr2, rand_scale, rand_mask);",
            "f2"  : "unsigned short {0} = fp32_to_fp16_rand({1}, lfsr0, lfsr1, lfsr2, rand_scale, rand_mask);",
            "i1"  : "char {0}           = fp32_to_int8({1});",
            "u1"  : "unsigned char {0}  = fp32_to_uint8({1});",
        },
        "nearest" : {
            "f2"  : "unsigned short {0} = fp32_to_fp16({1});",
            "i1"  : "char {0}           = fp32_to_int8({1});",
            "u1"  : "unsigned char {0}  = fp32_to_uint8({1});",
        },
    },
}

_is_finite = r"""
float {0};
asm("{{\n\t"
    ".reg .pred is_finite;\n\t"
    "testp.finite.f32 is_finite, %1;\n\t"
    "selp.f32 %0, 0F3f800000, 0F00000000, is_finite;\n\t"
    "}}" : "=f"({0}) : "f"({1}));
"""

# Note: binary operands come off the stack in reverse order
_float_ops = {
    "assign"  : (2, "*a_out = {0};\n        a_out += out_inc;" ),
    "add"     : (2, 'float {0} = {2} + {1};' ),
    "sub"     : (2, 'float {0} = {2} - {1};' ),
    "mul"     : (2, 'float {0} = {2} * {1};' ),
    "div"     : (2, 'float {0} = __fdividef({2}, {1});' ),
    "eq"      : (2, "float {0} = {2} == {1};"   ),
    "ne"      : (2, "float {0} = {2} != {1};"   ),
    "lt"      : (2, "float {0} = {2} <  {1};"   ),
    "le"      : (2, "float {0} = {2} <= {1};"   ),
    "gt"      : (2, "float {0} = {2} >  {1};"   ),
    "ge"      : (2, "float {0} = {2} >= {1};"   ),
    "minimum" : (2, "float {0} = fminf({2},{1});" ),
    "maximum" : (2, "float {0} = fmaxf({2},{1});" ),
    "pow"     : (2, "float {0} = powf({2},{1});"  ),
    "finite"  : (1, _is_finite ),
    "neg"     : (1, "float {0} = -{1};"         ),
    "abs"     : (1, "float {0} = abs({1});"     ),
    "sgn"     : (1, "float {0} = copysignf(1.0f, {1});"     ),
    "sqrt"    : (1, "float {0} = sqrtf({1});"   ),
    "sqr"     : (1, "float {0} = {1} * {1};"    ),
    "exp"     : (1, "float {0} = expf({1});"    ),
    "log"     : (1, "float {0} = logf({1});"    ),
    "exp2"    : (1, "float {0} = exp2f({1});"   ),
    "log2"    : (1, "float {0} = log2f({1});"   ),
    "sig"     : (1, "float {0} = 1.0f/(1.0f + expf(-{1}));"),
    "sig2"    : (1, "float {0} = 1.0f/(1.0f + exp2f(-{1}));"),
    "tanh"    : (1, "float {0} = tanhf({1});"   ),
    "tanh2"   : (1, "float {0} = (exp2f(2.0f*{1}) - 1.0f) / (exp2f(2.0f*{1}) + 1.0f);" ),
    "rand"    : (0, "float {0} = frand(lfsr0, lfsr1, lfsr2);"),
}

_reduction_ops = {
    "sum" : {
        "inits"      : "float {0} = 0.0f;",
        "ops"        : "{0} += {1};",
        "shfl_red"   : "{0} += __shfl_xor({0}, i);",
        "share1_red" : "sPartials[tid] += sPartials[tid + a];",
        "share2_red" : "{0} = sPartials[tid] + sPartials[tid + 32];",
    },
    "max" : {
        "inits"      : "float {0} = -FLT_MAX;",
        "ops"        : "{0} = fmaxf({0}, {1});",
        "shfl_red"   : "{0} = fmaxf({0}, __shfl_xor({0}, i));",
        "share1_red" : "sPartials[tid] = fmaxf(sPartials[tid], sPartials[tid + a]);",
        "share2_red" : "{0} = fmaxf(sPartials[tid], sPartials[tid + 32]);",
    },
    "min" : {
        "inits"      : "float {0} = FLT_MAX;",
        "ops"        : "{0} = fminf({0}, {1});",
        "shfl_red"   : "{0} = fminf({0}, __shfl_xor({0}, i));",
        "share1_red" : "sPartials[tid] = fminf(sPartials[tid], sPartials[tid + a]);",
        "share2_red" : "{0} = fminf(sPartials[tid], sPartials[tid + 32]);",
    },
    "argmax" : {
        "inits"     : "float {0} = -1.0f, max = -FLT_MAX;",
        "ops"       : "if ({1} > max) {{ max = {1}; {0} = i; }}",
        "shfl_red"  : "float max2 = __shfl_xor(max, i), argMax2 = __shfl_xor({0}, i);\n"
              "        if (max2 > max) {{ max = max2; {0} = argMax2; }}"
              "        else if (max2 == max && argMax2 < {0}) {{ {0} = argMax2; }}",
    },
    "argmin" : {
        "inits"     : "float {0} = -1.0f, min = FLT_MAX;",
        "ops"       : "if ({1} < min) {{ min = {1}; {0} = i; }}",
        "shfl_red"  : "float min2 = __shfl_xor(min, i), argMin2 = __shfl_xor({0}, i);\n"
              "        if (min2 < min) {{ min = min2; {0} = argMin2; }}"
              "        else if (min2 == min && argMin2 < {0}) {{ {0} = argMin2; }}",
    },
}

# import re
# import traceback as tb
# nrv_re = re.compile(r'gpu\.py$')
# def _get_trace():
#     caller = None
#     for frame in tb.extract_stack():
#         if nrv_re.search(frame[0]):
#             break
#         caller = (frame[0],frame[1])
#     return caller

def _get_module(template, template_vals):

    # print template, "\n\n"
    # for k in sorted(template_vals.keys()): print(k, template_vals[k])

    code = template % template_vals

    # f = open("kernel.cu", "w")
    # f = open("%s.cu" % template_vals["name"], "w")
    # print >>f, code
    # f.close()

    # print "Compiling %s %s" % (template_vals["name"], _get_trace())

    return SourceModule(code, options=[ "--use_fast_math" ], keep=False) #,"-G"

def _init_rand(template_vals):

    template_vals["common"].append(_common_urand_gen)
    template_vals["inits"].append(_init_rand_func)
    template_vals["finish"].append(_finish_rand_func)
    return True

@context_dependent_memoize
def _get_compound_kernel(type_args):

    post_reduction = False
    dup_reduction  = dict()
    placeholders   = set(("common","arguments","inits","finish","loads0","ops0"))
    stage_map      = dict()
    stage_type     = "loop"
    stage          = 0
    stack          = []
    threads        = type_args[-1][3]

    # Do a first pass over the stack to find out to which stage each
    # tensor and operation belong.
    for arg_i, arg in enumerate(type_args):

        arg_type = arg[0]
        if arg_type == "assign":

            # if we didn't start a new ew stage, we need a special output
            # stage for reduction
            if post_reduction:
                stage     += 1
                stage_type = "red_out"
                placeholders.add("ops%d" % stage)
                post_reduction = False

            stage_map[arg_i] = (stage,stage_type)
            for i in range(2):
                operand_i, operand = stack.pop()
                if operand[0] is ng.GPUTensor:
                    stage_map[operand_i] = (stage,stage_type)

        elif arg_type in _float_ops:

            # For each tensor argument asign the stage that it belongs to.
            for i in range(_float_ops[arg_type][0]):
                operand_i, operand = stack.pop()
                if operand[0] is ng.GPUTensor:
                    # If we are in post reduction and see a tensor we need to
                    # switch stages to an ew loop.
                    if post_reduction:
                        stage     += 1
                        stage_type = "loop"
                        placeholders.add("loads%d" % stage)
                        placeholders.add("ops%d"   % stage)
                        post_reduction = False

                stage_map[operand_i] = (stage,stage_type)

            # just append the temp float as a placeholder
            stack.append((-1,(float,)))

            # Tie this operation to a stage
            stage_map[arg_i] = (stage,stage_type)

        # Each time we do a reduction we need to setup a new elementwise loop
        elif arg_type in _reduction_ops:

            # It's possible to have back to back reductions.
            # If so start a new ew loop stage.
            if post_reduction:
                stage     += 1
                stage_type = "loop"
                placeholders.add("loads%d" % stage)
                placeholders.add("ops%d"   % stage)

            # Tie this operation to a stage
            stage_map[arg_i] = (stage,stage_type)

            # Tie a tensor to the stage if one precedes the reduction.
            operand_i, operand = stack.pop()
            if operand[0] is ng.GPUTensor:
                stage_map[operand_i] = (stage,stage_type)

            # just append the temp float as a placeholder
            stack.append((-1,(float,)))

            # generate a unique signature for this reduction op
            red_sig = []
            for i, a in enumerate(type_args):
                # find everything tied to this stage
                if i in stage_map and stage_map[i][0] == stage:
                    # for operations, just append the name
                    if type(a[0]) is str:
                        red_sig.append(a[0])
                    # For tensor or constant, append type and id.
                    # Note that constants have unique ids and will prevent
                    # duplicate detection.  Need to know diff between constants that
                    # can change or are actually static... save for another day.
                    # TODO: this has implications for cached execution plans.
                    else:
                        red_sig.append(tuple(a[0:2]))
            red_sig = tuple(red_sig)

            # Look for duplicate reductions
            if red_sig in dup_reduction:
                # remove duplicate placeholders
                placeholders.remove("loads%d" % stage)
                placeholders.remove("ops%d"   % stage)
                # print "dup: ", stage, arg[1], red_sig, dup_reduction[red_sig]
                # link the dup stage with the original
                dup_reduction[stage] = dup_reduction[red_sig]
            else:
                # tie each reduction signature to its stage
                dup_reduction[red_sig] = stage

                # finish building the reduction stage
                placeholders.add("shfl_red%d" % stage)
                if threads > 32:
                    placeholders.add("var_red%d"    % stage)
                    placeholders.add("share1_red%d" % stage)
                    placeholders.add("share2_red%d" % stage)

            # The ops section begins a new stage
            # We could try and find the longest common op string and reuse these ops
            # along with the reduction but it's not worth the complication.
            stage     += 1
            stage_type = "red_ops"
            placeholders.add("ops%d" % stage)

            post_reduction = True

        else:
            # build the stack with the operands
            stack.append((arg_i, arg))

    # print "\n".join(str(stage_map[i]) + " " + str(s) for i,s in enumerate(type_args))
    # print "\n"
    # print "\n".join(str(s) for s in placeholders)
    #exit()

    sig           = "P" # first param for rand_state
    stack         = []
    array_ids     = set()
    fp16In        = False
    rand_init     = False
    rand_func     = False
    current_stage = None
    stack         = []
    red_regsiters = {}
    template      = _ew_template
    template_vals = { "name" : "kernel_", "threads" : threads }
    for key in placeholders:
        template_vals[key] = []

    for arg_i, arg in enumerate(type_args):

        arg_type, arg_id = arg[0:2]

        stage, stage_type = stage_map[arg_i]

        # build out the template as we process operations (strings)
        if type(arg_type) is str:
            # don't build duplicate stages
            if stage not in dup_reduction:
                # build the template as the stage and stage_type combination changes
                if current_stage != stage_map[arg_i]:
                    current_stage = stage_map[arg_i]
                    template += _stage_template[stage_type].format(stage)
                # the reduction op shares the stage with its loop
                # so append that separately here.
                if arg_type in _reduction_ops:
                    if threads > 32:
                        template += _stage_template["red"].format(stage)
                    else:
                        template += _stage_template["red32"].format(stage)
            else:
                current_stage = stage_map[arg_i]

        # Array operands
        if arg_type is ng.GPUTensor:

            dtype = arg[2]

            #TODO: need to be able to handle more than 26 params..
            template_vals["name"] += dtype + chr(ord("A") + arg_id)

            if stage not in dup_reduction:

                # first arg is output array, don't put on stack
                if arg_i > 0:
                    stack.append("a%d" % arg_id)
                else:
                    out_dtype = dtype

                # 0: arg_id, 1: stage, 2: type, 3: cvt
                ew_dtype = _ew_types[dtype]
                fmt = (arg_id, stage, ew_dtype["type"], ew_dtype["cvt"])

                # First time we see a tensor initialize everything
                if arg_id not in array_ids:

                    array_ids.add(arg_id)
                    array_ids.add((arg_id,stage))

                    sig += "Pii"

                    # input tensors
                    if arg_i > 0:
                        ew_in = _ew_strings["in"]
                        loads = "loads%d" % stage
                        template_vals["arguments"].append(ew_in["arguments"].format(*fmt))
                        template_vals["inits"    ].append(ew_in["inits"    ].format(*fmt))
                        template_vals[loads      ].append(ew_in["loads"    ].format(*fmt))
                    # output tensor
                    else:
                        for key in ("arguments","inits"):
                            template_vals[key].append(_ew_strings["out"][key].format(*fmt))

                    if dtype == 'f2' and not fp16In:
                        template_vals["common"].append(_common_fp16_to_fp32)
                        fp16In = True

                # Subsequent times we see a tensor just initialize inits and loads
                # But only for arrays of diferent non-dup stages
                elif (arg_id,stage) not in array_ids:
                    array_ids.add((arg_id,stage))
                    ew_in = _ew_strings["in"]
                    loads = "loads%d" % stage
                    template_vals["inits"].append(ew_in["inits"].format(*fmt))
                    template_vals[loads  ].append(ew_in["loads"].format(*fmt))

        # Constant operands
        elif arg_type is float:

            sig  += "f"
            template_vals["name"] += "f4" + chr(ord("a") + arg_id)
            if stage not in dup_reduction:
                stack.append("c%d" % arg_id)
                ew_const = _ew_strings["const"]
                template_vals["arguments"].append(ew_const["arguments"].format(arg_id))

        # Operations (arg_type = op_name)
        else:

            template_vals["name"] += "_%s_" % arg_type

            if arg_type == "assign":

                ops = "ops%d" % stage

                # loop end condition for last stage
                sig += "i"
                template_vals["arguments"].append("const int n%d" % stage)

                # rounding mode
                if arg[2]:
                    mode = "random"
                    post = "rr"
                    sig += "i"
                    template_vals["arguments"].append("const int mantissa_bits")
                    if not rand_init:
                        rand_init = _init_rand(template_vals)
                    template_vals["inits"].append(_init_rand_round_func)
                else:
                    mode = "nearest"
                    post = "rn"

                out_val   = stack.pop()
                ew_round  = _ew_strings["round"][mode].get(out_dtype, None)
                ew_common = _common_round[mode].get(out_dtype, None)
                if ew_common:
                    template_vals["common"].append(ew_common)

                template_vals["name"] += post
                if ew_round:
                    round_val = "r%d" % arg_id
                    template_vals[ops].append(ew_round.format(round_val, out_val))
                else:
                    round_val = out_val

                template_vals[ops].append(_float_ops[arg_type][1].format(round_val))

            elif arg_type in _float_ops:

                if stage not in dup_reduction:

                    ops = "ops%d" % stage

                    (num_ops, op_code) = _float_ops[arg_type]

                    if arg_type == "rand":
                        if not rand_init:
                            rand_init = _init_rand(template_vals)
                        if not rand_func:
                            template_vals["common"].append(_common_frand)
                            rand_func = True

                    op_list = [ "r%d" % arg_id ]

                    #build the operands from the stack
                    for i in range(num_ops):
                        op_list.append(stack.pop())

                    template_vals[ops].append(op_code.format(*op_list))

                    stack.append(op_list[0])

            elif arg_type in _reduction_ops:

                # loop end condition for current stage
                # add regardless of duplicate reduction stage
                sig += "i"
                template_vals["arguments"].append("const int n%d" % stage)

                # if this is a duplicate reduction just push the previous
                # result back onto the stack.
                if stage in dup_reduction:
                    stack.append(red_regsiters[dup_reduction[stage]])
                # Otherwise fill out the reduction template
                else:

                    ops         = "ops%d"      % stage
                    shfl_red    = "shfl_red%d" % stage
                    red_arg     = "r%d"        % arg_id
                    red_strings = _reduction_ops[arg_type]
                    stack_arg   = stack.pop()

                    template_vals["inits" ].append(red_strings["inits"   ].format(red_arg))
                    template_vals[ops     ].append(red_strings["ops"     ].format(red_arg, stack_arg))
                    template_vals[shfl_red].append(red_strings["shfl_red"].format(red_arg))
                    if threads > 32:
                        var_red  = "var_red%d"    % stage
                        shr1_red = "share1_red%d" % stage
                        shr2_red = "share2_red%d" % stage
                        template_vals[var_red ].append(red_arg)
                        template_vals[shr1_red].append(red_strings["share1_red"].format(red_arg))
                        template_vals[shr2_red].append(red_strings["share2_red"].format(red_arg))

                    stack.append(red_arg)

                    # remember this register in case a duplicate needs it.
                    red_regsiters[stage] = red_arg

            else:
                raise ValueError("Bad op type.")

    template += _fin_template

    # convert lists to strings
    template_vals["common"]     = "\n".join(template_vals["common"])
    template_vals["arguments"]  = ",\n    ".join(template_vals["arguments"])
    template_vals["inits"]      = "\n    ".join(template_vals["inits"])
    template_vals["finish"]     = "\n".join(template_vals["finish"])
    for key in ("common","arguments","inits","finish"):
        placeholders.remove(key)

    # add the dynamic placeholders: loads#, ops#, reduction#
    for key in placeholders:
        template_vals[key]      = "\n        ".join(template_vals[key])

    module = _get_module(template, template_vals)
    kernel = module.get_function(template_vals["name"])
    kernel.name = template_vals["name"]
    kernel.prepare(sig)

    return kernel

# TODO: build a program wide DAG and only call this once at startup per assignment.
# TODO: allow multiple shape compatible assignments.
def call_compound_kernel(rand_state, *args):
    """
    Pass in a list of GPUTensor objects, constants and operators in postfix notation..

    C +=  2.5 * A * B + 1
    call_compound_ew_kernel(C, 2.5, A, "mul", B, "mul", 1, "add", C, "add", "assign")
    """
    out         = None
    arg_cnt     = 0
    op_cnt      = 0
    array_ids   = {}
    kernel_args = [ rand_state, ]
    type_args   = []
    shape_stack = []
    threads     = 32
    red_depth   = 0
    # Apply reduction constraints and determine thread axis
    # Blocks will be allocated counter to this axis
    # Also detect if this is a broadcast or transpose op.
    reduction = False
    broadcast = False
    transpose = False
    argminmax = False
    axis = 1
    for arg in args:
        if type(arg) is dict:
            if arg["op"] in _reduction_ops:

                if arg["op"] in ("argmax", "argmin"):
                    argminmax = True

                # To reduce a whole tensor (axis=None) reduce along each axis in succession.
                if arg.get("axis",None) not in (0,1):
                    raise ValueError("Only reduction along an axis currently supported")

                # Keep axis values consistent within the same kernel
                if reduction is True:
                    if arg["axis"] != axis:
                        raise ValueError("Reduction only allowed along one axis per kernel.")
                else:
                    reduction = True
                    axis = arg["axis"]

        elif isinstance(arg, ng.GPUTensor):
            if len(arg.shape) < 2 or arg.shape[0] == 1 or arg.shape[1] == 1:
                broadcast = True
            elif arg.is_trans:
                transpose = True


    # If reducing along axis 0 we need to reverse all strides.
    # Each block gets a column and the threads work down the columns.
    stride_order = 1 if axis == 1 else -1

    for arg in args:

        # Array operand
        if isinstance(arg, ng.GPUTensor):

            # use the more efficient dimensions if this is a plain ew op.
            if len(arg.shape) == 2 and (broadcast or reduction or transpose):
                shape   = arg.shape
                strides = arg.strides
            else:
                shape   = arg.shape_ew
                strides = arg.strides_ew

            # If same array is passed in multiple times to expression,
            # consolidate them into one kernel argument.
            if arg in array_ids:
                indx = array_ids[arg]
            else:

                # The first array passed in should be the output.
                # It's ok if this array is duplicated as the first instance
                # needs to be a mutable pointer.
                # A subsequent instance of out (if present) will be a const pointer.
                if out is None:
                    out  = arg
                    indx = arg_cnt
                else:
                    indx = array_ids[arg] = arg_cnt
                arg_cnt += 1

                # support transposed striding or reduction along an axis
                # let C pointer arithmetic handle itemsize for us
                strides = [s // arg.dtype.itemsize for s in strides[::stride_order]]

                # special case of reducing and outputing along axis=0
                if arg is out and axis == 0 and shape[0] == 1:
                    strides[0] = 1
                    strides[1] = 0
                else:
                    # support broadcast of a row vector
                    if shape[0] == 1: strides[0] = 0

                    # If we're traversing down the columns and this tensor has only one column,
                    # we preserve the col_stride to allow us to jump to the next row.
                    # This is probably a hack so maybe investigate this further.
                    if axis == 1:
                        # For the common case of traversing down the rows, zero the stride to
                        # support broadcast of column vector.
                        if shape[1] == 1: strides[1] = 0

                kernel_args.extend((arg.gpudata, strides[0], strides[1]))

            type_args.append((ng.GPUTensor, indx, arg.dtype.str[1:]))

            shape_stack.append(shape)

        # Constant operand
        elif type(arg) in (int, float):

            kernel_args.append(float(arg))
            type_args.append((float, arg_cnt))
            shape_stack.append((1,1))
            arg_cnt += 1

        # Operation
        elif type(arg) is dict:

            op_name = arg["op"]

            if op_name in _float_ops:

                # we need to do the shape arithemtic for the current operation
                max_shape = [1,1]
                for op_num in range(_float_ops[op_name][0]):
                    shape = shape_stack.pop()
                    for i in range(2):
                        if shape[i] != max_shape[i]:
                            # support broadcast
                            # TODO: don't allow output tensor itself to be broadcastable.
                            # The final output is fine as a broadcast, for example assigning a constant.
                            # You just dont want a tensor being assigned to a smaller shape.
                            if shape[i] == 1 or max_shape[i] == 1:
                                max_shape[i] = max(max_shape[i], shape[i])
                            else:
                                raise TypeError("Input shape:%s not compatible" % (shape,))

                if op_name == "assign":

                    # the axis dim is the thread loop stop condition
                    kernel_args.append(max_shape[axis])

                    rounding = out.rounding

                    # support rounding to arbitrary mantissa size
                    if rounding:
                        # convert bool to some default mantissa
                        if rounding is True:
                            rounding = 10
                        elif out.dtype.type is np.float32:
                            rounding = min(rounding,15)
                        elif out.dtype.type is np.float16:
                            rounding = min(rounding,10)

                        kernel_args.append(max(rounding,1))

                    # speed up deep reduction by using more than 32 threads
                    if reduction and not argminmax:
                        if   red_depth >= 4096: threads = 1024
                        elif red_depth >= 2048: threads = 512
                        elif red_depth >= 1024: threads = 256
                        elif red_depth >=  512: threads = 128
                        elif red_depth >=  256: threads = 64

                    # speed up deep broadcast by using more than 32 threads
                    elif not (reduction or transpose) and max_shape[1] >= 512:
                        threads = 256

                    type_args.append((op_name, op_cnt, rounding > 0, threads))

                else:
                    type_args.append((op_name, op_cnt))
                    shape_stack.append(max_shape)

            elif op_name in _reduction_ops:

                shape = list(shape_stack.pop())

                red_depth = max(red_depth, shape[axis])

                # Allow a new axis size if doing post reduction broadcast.
                # So we need to know the axis size prior to reduction.
                kernel_args.append(shape[axis])
                type_args.append((op_name, op_cnt))

                # reduce the current shape
                shape[axis] = 1

                # udpate the current shape state
                shape_stack.append(shape)

            else:
                raise TypeError("%s is not a valid operation" % op_name)

            op_cnt += 1

        else:
            raise TypeError("args must be instance of GPUTensor, int, float, or dict (for operators)")

    # print "\n".join(str(s) for s in args)
    # print "\n"
    # print "\n".join(str(s) for s in kernel_args)
    # print "\n"
    # print "\n".join(str(s) for s in type_args)

    # get or create the kernel in the memoize cache
    kernel = _get_compound_kernel(tuple(type_args))

    #import ipdb; ipdb.set_trace()

    shared = threads * 4 if reduction and threads > 32 else 0

    if out.backend.bench:
        repeat = out.backend.bench
        start, end = ng._get_events()
        start.record(out.backend.stream)
    else:
        repeat = 1

    for r in range(repeat):

        # call the kernel with the number of blocks set as the size of the off-axis
        # Maxwell does well with 32 thread sized blocks, no need to autotune.
        #for a in kernel_args: print a
        kernel.prepared_async_call((max_shape[1-axis],1,1), (threads,1,1), out.backend.stream, *kernel_args, shared_size=shared)

    if out.backend.bench:
        end.record(out.backend.stream)
        end.synchronize()
        msecs = end.time_since(start) / repeat
        print("%7.3f msecs shape(%d,%d) blk,thd(%d,%d) %s" % (msecs, max_shape[0], max_shape[1], max_shape[1-axis], threads, kernel.name))

    return out

_compensated_sum = r"""

%(common)s

__global__ void compensated_sum(unsigned* rand_state,
          %(type)s* a_sum,
          %(type)s* a_cmp,
    const %(type)s* a_add,
    float cmp_scale, float add_scale,
    int row_strd, int col_strd, int n, int mantissa_bits)
{
    const int tid = threadIdx.x;
    const int bid = blockIdx.x;

    int offset = bid * row_strd + tid * col_strd;
    int inc    = 32 * col_strd;

    a_sum += offset;
    a_cmp += offset;
    a_add += offset;

    %(inits)s

    for (int i = tid; i < n; i += 32)
    {
        float s32 = %(cvt)s(__ldg((const %(type)s*)a_sum));
        float c32 = %(cvt)s(__ldg((const %(type)s*)a_cmp));
        float a32 = %(cvt)s(__ldg(a_add));

        // Adjust amount to add by previous compensation
        float y32 = a32 * add_scale - c32 * cmp_scale;

        // Do the accumulation and truncate to the storage type
        float rnd_sum = s32 + y32;
        %(rnd_sum)s

        // Convert accumulation back to fp32 so we can do more math on it
        float t32 = %(cvt)s(t16);

        // recover the low order bits that were lost in the truncation
        float rnd_cmp = (t32 - s32) - y32;
        %(rnd_cmp)s

        *a_sum = t16;
        *a_cmp = c16;

        a_sum += inc;
        a_cmp += inc;
        a_add += inc;
    }
    %(finish)s
}
"""

@context_dependent_memoize
def _get_compensated_sum_kernel(dtype, rounding):

    template_vals = dict()
    for key in ("common", "inits", "finish"):
        template_vals[key] = ""

    if dtype == "f2":
        template_vals["common"] += _common_fp16_to_fp32

    if rounding:
        template_vals["common"] += _common_urand_gen
        template_vals["common"] += _common_round["nearest"].get(dtype,"")
        template_vals["inits" ] += _init_rand_func + _init_rand_round_func
        template_vals["finish"] += _finish_rand_func
        mode = "random"
    else:
        mode = "nearest"

    template_vals["common"] += _common_round[mode].get(dtype,"")

    template_vals["type"] = _ew_types[dtype]["type"]
    template_vals["cvt"]  = _ew_types[dtype]["cvt"]

    no_op = "float {0} = {1};"

    rnd_sum = _ew_strings["round"][  mode   ].get(dtype, no_op)
    rnd_cmp = _ew_strings["round"]["nearest"].get(dtype, no_op)

    template_vals["rnd_sum"] = rnd_sum.format("t16","rnd_sum")
    template_vals["rnd_cmp"] = rnd_cmp.format("c16","rnd_cmp")

    code = _compensated_sum % template_vals

    # f = open("compensated_sum.cu", "w")
    # print >>f, code
    # f.close()

    module = SourceModule(code)
    kernel = module.get_function("compensated_sum")
    kernel.prepare("PPPPffiiii")
    return kernel
