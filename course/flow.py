# -*- coding: utf-8 -*-

from __future__ import division

__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

from django.shortcuts import (  # noqa
        render, get_object_or_404, redirect)
from django.contrib import messages
from django.core.exceptions import PermissionDenied, SuspiciousOperation
from django.db import transaction
from django.utils.safestring import mark_safe

import re

from course.models import (
        participation_role,
        FlowSession, FlowPageData, FlowPageVisit, FlowPageVisitGrade,
        flow_permission,
        GradeChange)

from course.utils import (
        FlowContext, FlowPageContext,
        instantiate_flow_page_with_ctx)


# {{{ grade page visit

def grade_page_visit(visit, visit_grade_model=FlowPageVisitGrade, grade_data=None):
    if not visit.is_graded_answer:
        raise RuntimeError("cannot grade ungraded answer")

    flow_session = visit.flow_session
    course = flow_session.course
    page_data = visit.page_data

    from course.content import (
            get_course_repo,
            get_course_commit_sha,
            get_flow_commit_sha,
            get_flow_desc,
            get_flow_page_desc,
            instantiate_flow_page)

    repo = get_course_repo(course)

    course_commit_sha = get_course_commit_sha(
            course, flow_session.participation)

    flow_desc_pre = get_flow_desc(repo, course,
            flow_session.flow_id, course_commit_sha)

    flow_commit_sha = get_flow_commit_sha(
            course, flow_session.participation, flow_desc_pre,
            visit.flow_session)

    flow_desc = get_flow_desc(repo, course,
            flow_session.flow_id, flow_commit_sha)

    page_desc = get_flow_page_desc(
            flow_session.flow_id,
            flow_desc,
            page_data.group_id, page_data.page_id)

    page = instantiate_flow_page(
            location="flow '%s', group, '%s', page '%s'"
            % (flow_session.flow_id, page_data.group_id, page_data.page_id),
            repo=repo, page_desc=page_desc,
            commit_sha=flow_commit_sha)

    from course.page import PageContext
    grading_page_context = PageContext(
            course=course,
            repo=repo,
            commit_sha=flow_commit_sha)

    answer_feedback = page.grade(
            grading_page_context, visit.page_data.data,
            visit.answer, grade_data=grade_data)

    grade = visit_grade_model()
    grade.visit = visit
    grade.grade_data = grade_data
    grade.max_points = page.max_points(visit.page_data)

    if answer_feedback is not None:
        grade.correctness = answer_feedback.correctness
        grade.feedback = answer_feedback.as_json()

    grade.save()

# }}}


# {{{ finish flow

def get_flow_session_graded_answers_qset(flow_session):
    from django.db.models import Q
    qset = (FlowPageVisit.objects
            .filter(flow_session=flow_session)
            .filter(Q(answer__isnull=False) | Q(is_synthetic=True)))

    if not flow_session.in_progress:
        # Ungraded answers *can* show up in non-in-progress flows as a result
        # of a race between a 'save' and the 'end session'. If this happens,
        # we'll go ahead and ignore those.
        qset = qset.filter(is_graded_answer=True)

    return qset


def assemble_answer_visits(flow_session):
    answer_visits = [None] * flow_session.page_count

    answer_page_visits = (
            get_flow_session_graded_answers_qset(flow_session)
            .order_by("visit_time"))

    for page_visit in answer_page_visits:
        answer_visits[page_visit.page_data.ordinal] = page_visit

        if not flow_session.in_progress:
            # This is redundant with the answers being marked as
            # final at the end of a flow, but that's OK.
            #
            # Note that this change is generally not persisted.
            page_visit.is_graded_answer = True

    return answer_visits


def count_answered(fctx, flow_session, answer_visits):
    all_page_data = (FlowPageData.objects
            .filter(flow_session=flow_session)
            .order_by("ordinal"))

    answered_count = 0
    unanswered_count = 0
    for i, page_data in enumerate(all_page_data):
        assert i == page_data.ordinal

        if answer_visits[i] is not None:
            answer_data = answer_visits[i].answer
        else:
            answer_data = None

        page = instantiate_flow_page_with_ctx(fctx, page_data)
        if page.expects_answer():
            if answer_data is None:
                unanswered_count += 1
            else:
                answered_count += 1

    return (answered_count, unanswered_count)


class GradeInfo(object):
    def __init__(self,
            points, max_points,
            fully_correct_count, partially_correct_count, incorrect_count):
        self.points = points
        self.max_points = max_points
        self.fully_correct_count = fully_correct_count
        self.partially_correct_count = partially_correct_count
        self.incorrect_count = incorrect_count

    def points_percent(self):
        if self.max_points is None or self.max_points == 0:
            if self.points == 0:
                return 100
            else:
                return 0
        else:
            return 100*self.points/self.max_points

    def missed_points_percent(self):
        return 100 - self.points_percent()

    def total_count(self):
        return (self.fully_correct_count
                + self.partially_correct_count
                + self.incorrect_count)

    def fully_correct_percent(self):
        return 100*self.fully_correct_count/self.total_count()

    def partially_correct_percent(self):
        return 100*self.partially_correct_count/self.total_count()

    def incorrect_percent(self):
        return 100*self.incorrect_count/self.total_count()


def gather_grade_info(fctx, answer_visits):
    all_page_data = (FlowPageData.objects
            .filter(flow_session=fctx.flow_session)
            .order_by("ordinal"))

    points = 0
    max_points = 0
    fully_correct_count = 0
    partially_correct_count = 0
    incorrect_count = 0

    for i, page_data in enumerate(all_page_data):
        assert i == page_data.ordinal

        if answer_visits[i] is None:
            # page did not expect an answer
            continue

        grade = answer_visits[i].get_most_recent_grade()
        assert grade is not None

        from course.page import AnswerFeedback
        feedback = AnswerFeedback.from_json(grade.feedback)

        if feedback is None or feedback.correctness is None:
            return None

        max_points += grade.max_points
        points += grade.max_points*feedback.correctness

        if feedback.correctness == 1:
            fully_correct_count += 1
        elif feedback.correctness == 0:
            incorrect_count += 1
        else:
            partially_correct_count += 1

    return GradeInfo(
            points=points,
            max_points=max_points,
            fully_correct_count=fully_correct_count,
            partially_correct_count=partially_correct_count,
            incorrect_count=incorrect_count)


def grade_page_visits(fctx, flow_session, answer_visits):
    for i in range(len(answer_visits)):
        answer_visit = answer_visits[i]

        if answer_visit is not None:
            answer_visit.is_graded_answer = True
            answer_visit.save()

        else:
            page_data = flow_session.page_data.get(ordinal=i)
            page = instantiate_flow_page_with_ctx(fctx, page_data)

            if not page.expects_answer():
                continue

            # Create a synthetic visit to attach a grade
            answer_visit = FlowPageVisit()
            answer_visit.flow_session = flow_session
            answer_visit.page_data = page_data
            answer_visit.is_synthetic = True
            answer_visit.answer = None
            answer_visit.is_graded_answer = True
            answer_visit.save()

            answer_visits[i] = answer_visit

        if answer_visit is not None:
            grade_page_visit(answer_visit)


def finish_flow_session(fctx, flow_session):
    if not flow_session.in_progress:
        raise RuntimeError("Can't end a session that's already ended")

    answer_visits = assemble_answer_visits(flow_session)

    (answered_count, unanswered_count) = count_answered(
            fctx, fctx.flow_session, answer_visits)

    is_graded_flow = bool(answered_count + unanswered_count)

    if is_graded_flow:
        grade_page_visits(fctx, flow_session, answer_visits)

    # ORDERING RESTRICTION: Must grade pages before gathering grade info

    grade_info = gather_grade_info(fctx, answer_visits)

    comment = None

    if grade_info is not None:
        points = grade_info.points

        if hasattr(fctx.stipulations, "credit_percent"):
            comment = "Counted at %.1f%% of %.1f points" % (
                    fctx.stipulations.credit_percent, points)
            points = points * fctx.stipulations.credit_percent / 100
    else:
        points = None

    from django.utils.timezone import now
    flow_session.completion_time = now()
    flow_session.in_progress = False

    if grade_info is not None:
        flow_session.points = points
        flow_session.max_points = grade_info.max_points
    else:
        flow_session.points = None
        flow_session.max_points = None

    flow_session.result_comment = comment
    flow_session.save()

    if is_graded_flow and fctx.participation is not None and grade_info is not None:
        from course.models import get_flow_grading_opportunity
        gopp = get_flow_grading_opportunity(
                fctx.course, fctx.flow_identifier, fctx.flow_desc)

        from course.models import grade_state_change_types
        gchange = GradeChange()
        gchange.opportunity = gopp
        gchange.participation = fctx.participation
        gchange.state = grade_state_change_types.graded
        gchange.points = points
        gchange.max_points = grade_info.max_points
        # creator left as NULL
        gchange.flow_session = flow_session
        gchange.comment = comment
        gchange.save()

    return grade_info

# }}}


# {{{ view: start flow

RESUME_RE = re.compile("^resume_([0-9]+)$")


@transaction.atomic
def start_flow(request, course_identifier, flow_identifier):
    fctx = FlowContext(request, course_identifier, flow_identifier)

    from course.models import flow_permission
    if flow_permission.view not in fctx.permissions:
        raise PermissionDenied()

    have_in_progress_session = (FlowSession.objects
            .filter(
                participation=fctx.participation,
                flow_id=fctx.flow_identifier,
                in_progress=True,
                participation__isnull=False,
                )).count() > 0
    past_sessions = (FlowSession.objects
            .filter(
                participation=fctx.participation,
                flow_id=fctx.flow_identifier,
                participation__isnull=False)
           .order_by("start_time"))
    past_session_count = past_sessions.count()

    if hasattr(fctx.stipulations, "allowed_session_count"):
        allowed_another_session = (
                past_session_count < fctx.stipulations.allowed_session_count)
    else:
        allowed_another_session = True

    if request.method == "POST":
        from course.content import set_up_flow_session_page_data

        resume_match = None
        for post_key in request.POST:
            resume_match = RESUME_RE.match(post_key)
            if resume_match is not None:
                break

        if resume_match is not None:
            resume_session_id = int(resume_match.group(1))

            resume_session = get_object_or_404(FlowSession, pk=resume_session_id)

            if resume_session.participation != fctx.participation:
                raise PermissionDenied("not your session")

            if resume_session.participation is None:
                raise PermissionDenied("can't resume anonymous session")

            if resume_session.flow_id != fctx.flow_identifier:
                raise SuspiciousOperation("flow id mismatch on resume")

            if not (flow_permission.view_past in fctx.permissions
                    or resume_session.in_progress):
                raise PermissionDenied("not allowed to resume session")

            request.session["flow_session_id"] = resume_session_id

            return redirect("course.flow.view_flow_page",
                    course_identifier,
                    flow_identifier,
                    0)

        elif ("start_no_credit" in request.POST
                or "start_credit" in request.POST):

            if not allowed_another_session:
                raise PermissionDenied("new session would exceed "
                        "allowed session count limit exceed")

            if have_in_progress_session:
                raise PermissionDenied("cannot start flow when other flow "
                        "is already in progress")

            session = FlowSession()
            session.course = fctx.course
            session.participation = fctx.participation
            session.active_git_commit_sha = fctx.flow_commit_sha.decode()
            session.flow_id = flow_identifier
            session.in_progress = True
            session.for_credit = "start_credit" in request.POST
            session.save()

            request.session["flow_session_id"] = session.id

            page_count = set_up_flow_session_page_data(fctx.repo, session,
                    course_identifier, fctx.flow_desc, fctx.flow_commit_sha)
            session.page_count = page_count
            session.save()

            return redirect("course.flow.view_flow_page",
                    course_identifier,
                    flow_identifier,
                    0)

        else:
            raise SuspiciousOperation("unrecognized POST action")

    else:
        may_start_credit = (
                not have_in_progress_session
                and allowed_another_session
                and flow_permission.start_credit in fctx.permissions)
        may_start_no_credit = (
                not have_in_progress_session
                and allowed_another_session
                and flow_permission.start_no_credit in fctx.permissions)
        may_review = (
                flow_permission.view_past in fctx.permissions)

        if hasattr(fctx.flow_desc, "grade_aggregation_strategy"):
            from course.models import GRADE_AGGREGATION_STRATEGY_CHOICES
            grade_aggregation_strategy_text = (
                    dict(GRADE_AGGREGATION_STRATEGY_CHOICES)
                    [fctx.flow_desc.grade_aggregation_strategy])
        else:
            grade_aggregation_strategy_text = None

        return render(request, "course/flow-start.html", {
            "participation": fctx.participation,
            "course_desc": fctx.course_desc,
            "course": fctx.course,
            "flow_desc": fctx.flow_desc,
            "role": fctx.role,
            "participation_role": participation_role,
            "grade_aggregation_strategy":
            grade_aggregation_strategy_text,
            "flow_identifier": flow_identifier,

            "may_start_credit": may_start_credit,
            "may_start_no_credit": may_start_no_credit,
            "may_review": may_review,

            "past_sessions": past_sessions,
            "stipulations": fctx.stipulations,
            })

# }}}


# {{{ view: flow page

def find_current_flow_session(request, flow_identifier):
    flow_session = None
    flow_session_id = request.session.get("flow_session_id")

    if flow_session_id is not None:
        flow_sessions = list(FlowSession.objects.filter(id=flow_session_id))

        if flow_sessions and flow_sessions[0].flow_id == flow_identifier:
            flow_session, = flow_sessions

    return flow_session


def add_buttons_to_form(fpctx, form):
    from crispy_forms.layout import Submit
    form.helper.add_input(
            Submit("save", "Save answer",
                css_class="col-lg-offset-2 cf-save-button"))

    if fpctx.will_receive_feedback():
        if flow_permission.change_answer in fpctx.permissions:
            form.helper.add_input(
                    Submit(
                        "submit", "Submit answer for grading",
                        accesskey="g", css_class="cf-save-button"))
        else:
            form.helper.add_input(Submit("submit", "Submit final answer"))
    else:
        # Only offer 'save and move on' if student will receive no feedback
        if fpctx.page_data.ordinal + 1 < fpctx.flow_session.page_count:
            form.helper.add_input(
                    Submit("save_and_next",
                        mark_safe("Save answer and move on &raquo;"),
                        css_class="cf-save-button"))
        else:
            form.helper.add_input(
                    Submit("save_and_finish",
                        mark_safe("Save answer and finish &raquo;"),
                        css_class="cf-save-button"))

    return form


def get_pressed_button(form):
    buttons = ["save", "save_and_next", "save_and_finish", "submit"]
    for button in buttons:
        if button in form.data:
            return button

    raise SuspiciousOperation("could not find which button was pressed")


def view_flow_page(request, course_identifier, flow_identifier, ordinal):
    flow_session = find_current_flow_session(request, flow_identifier)

    if flow_session is None:
        messages.add_message(request, messages.WARNING,
                "No in-progress session record found for this flow. "
                "Redirected to flow start page.")

        return redirect("course.flow.start_flow",
                course_identifier,
                flow_identifier)

    fpctx = FlowPageContext(request, course_identifier, flow_identifier,
            ordinal, flow_session)

    page_context = fpctx.page_context
    page_data = fpctx.page_data

    if flow_permission.view not in fpctx.permissions:
        raise PermissionDenied("not allowed to view flow")

    if request.method == "POST":
        if "finish" in request.POST:
            return redirect("course.flow.finish_flow_session_view",
                    course_identifier, flow_identifier)
        else:
            # reject answer update if flow is not in-progress
            if not flow_session.in_progress:
                raise PermissionDenied("session is not in progress")

            # reject if previous answer was final
            if (fpctx.prev_answer_visit is not None
                    and fpctx.prev_answer_visit.is_graded_answer
                    and flow_permission.change_answer not in fpctx.permissions):
                raise PermissionDenied("already have final answer")

            form, form_html = fpctx.page.post_form(
                    fpctx.page_context, fpctx.page_data.data,
                    post_data=request.POST, files_data=request.POST)

            pressed_button = get_pressed_button(form)

            if form.is_valid():
                # {{{ form validated, process answer

                messages.add_message(request, messages.INFO,
                        "Answer saved.")

                page_visit = FlowPageVisit()
                page_visit.flow_session = fpctx.flow_session
                page_visit.page_data = fpctx.page_data
                page_visit.remote_address = request.META['REMOTE_ADDR']

                page_visit.answer = fpctx.page.answer_data(
                        fpctx.page_context, fpctx.page_data.data,
                        form)
                page_visit.is_graded_answer = pressed_button == "submit"
                page_visit.save()

                answer_was_graded = page_visit.is_graded_answer
                may_change_answer = (
                        not answer_was_graded
                        or flow_permission.change_answer in fpctx.permissions)

                feedback = fpctx.page.grade(
                        page_context, page_data.data, page_visit.answer,
                        grade_data=None)

                if page_visit.is_graded_answer:
                    grade = FlowPageVisitGrade()
                    grade.visit = page_visit
                    grade.max_points = fpctx.page.max_points(page_data.data)

                    if feedback is not None:
                        grade.correctness = feedback.correctness
                        grade.feedback = feedback.as_json()

                    grade.save()

                    del grade

                if (pressed_button == "save_and_next"
                        and not fpctx.will_receive_feedback()):
                    return redirect("course.flow.view_flow_page",
                            course_identifier,
                            flow_identifier,
                            fpctx.ordinal + 1)
                elif (pressed_button == "save_and_finish"
                        and not fpctx.will_receive_feedback()):
                    return redirect("course.flow.finish_flow_session_view",
                            course_identifier, flow_identifier)
                else:
                    form, form_html = fpctx.page.make_form(
                            page_context, page_data.data,
                            page_visit.answer, not may_change_answer)

                    # continue at common flow page generation below

                # }}}

                del page_visit

            else:
                # form did not validate

                fpctx.create_visit(request)

                answer_was_graded = False
                may_change_answer = True
                # because we were allowed this far in by the check above

                feedback = None

                # continue at common flow page generation below

    else:
        fpctx.create_visit(request)

        if fpctx.prev_answer_visit is not None:
            answer_was_graded = fpctx.prev_answer_visit.is_graded_answer
        else:
            answer_was_graded = False

        may_change_answer = (
                (not answer_was_graded
                    or flow_permission.change_answer in fpctx.permissions)

                # can happen if no answer was ever saved
                and fpctx.flow_session.in_progress)

        if fpctx.page.expects_answer():
            if fpctx.prev_answer_visit is not None:
                answer_data = fpctx.prev_answer_visit.answer
                feedback = fpctx.prev_answer_visit.get_most_recent_feedback()
            else:
                answer_data = None
                feedback = None

            form, form_html = fpctx.page.make_form(
                    page_context, page_data.data,
                    answer_data, not may_change_answer)
        else:
            form = None
            form_html = None
            feedback = None

    # start common flow page generation

    # defined at this point:
    # form, form_html, may_change_answer, answer_was_graded, feedback

    if form is not None and may_change_answer:
        form = add_buttons_to_form(fpctx, form)

    show_correctness = None
    show_answer = None

    shown_feedback = None

    if fpctx.page.expects_answer() and answer_was_graded:
        show_correctness = flow_permission.see_correctness in fpctx.permissions
        show_answer = flow_permission.see_answer in fpctx.permissions

        if show_correctness or show_answer:
            shown_feedback = feedback

    title = fpctx.page.title(page_context, page_data.data)
    body = fpctx.page.body(page_context, page_data.data)

    # {{{ render flow page

    if form is not None and form_html is None:
        from crispy_forms.utils import render_crispy_form
        from django.template import RequestContext
        context = RequestContext(request, {})
        form_html = render_crispy_form(form, context=context)
        del context

    args = {
        "course": fpctx.course,
        "course_desc": fpctx.course_desc,
        "flow_identifier": fpctx.flow_identifier,
        "flow_desc": fpctx.flow_desc,
        "ordinal": fpctx.ordinal,
        "page_data": fpctx.page_data,
        "percentage": fpctx.percentage_done,
        "flow_session": fpctx.flow_session,
        "participation": fpctx.participation,

        "role": fpctx.role,
        "participation_role": participation_role,

        "title": title, "body": body,
        "form": form,
        "form_html": form_html,
        "feedback": shown_feedback,
        "show_correctness": show_correctness,
        "may_change_answer": may_change_answer,
        "will_receive_feedback": fpctx.will_receive_feedback(),
        "show_answer": show_answer,
    }

    if fpctx.page.expects_answer():
        args["max_points"] = fpctx.page.max_points(fpctx.page_data)

    return render(request, "course/flow-page.html", args)

    # }}}

# }}}


# {{{ view: finish flow

@transaction.atomic
def finish_flow_session_view(request, course_identifier, flow_identifier):
    flow_session = find_current_flow_session(request, flow_identifier)

    if flow_session is None:
        messages.add_message(request, messages.WARNING,
                "No session record found for this flow. "
                "Redirected to flow start page.")

        return redirect("course.flow.start_flow",
                course_identifier,
                flow_identifier)

    fctx = FlowContext(request, course_identifier, flow_identifier,
            flow_session=flow_session)

    answer_visits = assemble_answer_visits(flow_session)

    from course.content import markup_to_html
    completion_text = markup_to_html(
            fctx.course, fctx.repo, fctx.flow_commit_sha,
            fctx.flow_desc.completion_text)

    (answered_count, unanswered_count) = count_answered(
            fctx, fctx.flow_session, answer_visits)

    def render_finish_response(template, **kwargs):
        render_args = {
            "course": fctx.course,
            "course_desc": fctx.course_desc,
            "flow_identifier": fctx.flow_identifier,
            "flow_desc": fctx.flow_desc,
            "participation": fctx.participation,
            "role": fctx.role,
            "participation_role": participation_role,
        }

        render_args.update(kwargs)
        return render(request, template, render_args)

    if request.method == "POST":
        if "submit" not in request.POST:
            raise SuspiciousOperation("odd POST parameters")

        if not flow_session.in_progress:
            raise PermissionDenied("Can't end a session that's already ended")

        # Actually end the flow session

        request.session["flow_session_id"] = None

        grade_info = finish_flow_session(fctx, flow_session)

        if answered_count + unanswered_count:
            # This is a graded flow.

            if grade_info is None:
                messages.add_message(request, messages.INFO,
                        "A grade for your work has not yet been assigned. "
                        "Please check back later for grade information.")

                return render_finish_response(
                        "course/flow-completion.html",
                        last_page_nr=None,
                        completion_text=completion_text)

            return render_finish_response(
                    "course/flow-completion-grade.html",
                    completion_text=completion_text,
                    grade_info=grade_info)

        else:
            # {{{ no grade

            return render_finish_response(
                    "course/flow-completion.html",
                    last_page_nr=None,
                    completion_text=completion_text)

            # }}}

    if (answered_count + unanswered_count == 0
            and fctx.flow_commit_sha == fctx.course_commit_sha):
        # Not serious--no questions in flow, and no new version available.
        # No need to end the flow visit.

        return render_finish_response(
                "course/flow-completion.html",
                last_page_nr=fctx.page_count-1,
                completion_text=completion_text)

    elif not flow_session.in_progress:
        # Just reviewing: re-show grades.
        grade_info = gather_grade_info(fctx, answer_visits)

        return render_finish_response(
                "course/flow-completion-grade.html",
                completion_text=completion_text,
                grade_info=grade_info)

    else:
        # confirm ending flow
        return render_finish_response(
                "course/flow-confirm-completion.html",
                last_page_nr=fctx.page_count-1,
                answered_count=answered_count,
                unanswered_count=unanswered_count,
                total_count=answered_count+unanswered_count)

# }}}

# vim: foldmethod=marker
