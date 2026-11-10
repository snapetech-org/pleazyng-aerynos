/* QA-only fault injection: reject the experimental render API, forward all else. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdio.h>
#include <string.h>
#include <mpv/render.h>
int mpv_render_context_create(mpv_render_context **res, mpv_handle *mpv, mpv_render_param *params) {
    for (mpv_render_param *p = params; p && p->type; ++p) {
        if (p->type == MPV_RENDER_PARAM_API_TYPE && p->data && !strcmp(p->data, "opengl-next")) {
            fprintf(stderr, "QA: rejecting opengl-next to exercise compatibility fallback\n");
            return MPV_ERROR_UNSUPPORTED;
        }
    }
    int (*real_create)(mpv_render_context **, mpv_handle *, mpv_render_param *) = dlsym(RTLD_NEXT, "mpv_render_context_create");
    return real_create(res, mpv, params);
}
