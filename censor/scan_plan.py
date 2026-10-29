"""One cache/explicit-request contract for direct and discovered video inputs."""


def reusable_result(service, path, options, jobs=None):
    if options.force or options.recheck_manual:
        return None
    result = service.cached_result(path, options)
    if result is not None and jobs is not None:
        # Completing a reused pending item matters just as much as a new probe:
        # otherwise resume keeps finding the same already-completed work.
        jobs.finished(result)
        jobs.finish_sources([path])
    return result


def should_defer(priority, options, *, authorized=False, manual_requested=False):
    return (priority == 2 and not authorized and not manual_requested
            and not options.force and not options.recheck_manual)


def partition_reusable(service, paths, options, *, jobs=None, should_stop=None):
    todo, reused = [], []
    for path in dict.fromkeys(map(str, paths)):
        if should_stop and should_stop():
            break
        cached = reusable_result(service, path, options, jobs)
        if cached is None:
            todo.append(path)
        else:
            reused.append(cached)
    return todo, reused
