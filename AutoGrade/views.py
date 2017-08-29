from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth import login, authenticate
from django.shortcuts import render, redirect
from django.http import HttpResponse, Http404
from django.contrib.auth.models import User
from django.views.decorators.csrf import csrf_exempt
from django.core import serializers
from django.utils import timezone
from django.utils.encoding import smart_str
from django.http import JsonResponse

from django.http import HttpResponseRedirect
from django.core.urlresolvers import reverse

from .models import Student, Course, Assignment, Submission
from .forms import SignUpForm, EnrollForm
from django.contrib import messages
from .grader import run_student_tests, write_student_log

import mimetypes
import json
import zipfile
import shutil
import logging
import os
import sys
import re
import time
import urllib
from datetime import datetime

@login_required(login_url='login')
def home(request):
    user = request.user
    student = Student.objects.filter(user=user).first()

    if user.is_staff or user.is_superuser:
        return HttpResponseRedirect(reverse('admin:index'))
    
    form = EnrollForm()
    if request.method == "POST":
        form = EnrollForm(request.POST)

        if form.is_valid():
            secret_key = form.cleaned_data['secret_key']

            course = Course.objects.filter(enroll_key=secret_key)
            if course.exists():
                course = course[0]
                already_registered = Student.objects.filter(pk=student.id, courses__id=course.id).exists()
                print (already_registered)
                if already_registered: 
                    messages.warning1(request, 'You have already registered that course')
                else:
                    student.courses.add(course)
                    student.save()
                    messages.success(request, 'You are successfully registered to the course')

            else:
                form.add_error('secret_key', "Invalid Secret Key")
            redirect('home')

    errors = form.errors or None
    return render(request,
                  'home.html',
                  {
                      'courses': student.courses.all(),
                      'form': form,
                      'errors': errors,
                      'student': student
                  }
                  )


def signup(request):
    if request.method == 'POST':
        form = SignUpForm(request.POST)
        if form.is_valid():
            user = form.save()
            user.refresh_from_db()
            user.save()
            
            student = Student.objects.create(user=user)
            student.save()
            
            raw_password = form.cleaned_data.get('password1')
            user = authenticate(username=user.username, password=raw_password)
            login(request, user)
            request.session['username'] = user.username
            return redirect('home')
    else:
        form = SignUpForm()
    return render(request, 'signup.html', {'form': form})

def download(request):
    # TODO: break when user try to download Instructor Test file
    submission_id = request.GET.get('sid')
    assignment_id = request.GET.get('aid')
    action = request.GET.get('action')

    # Check 
    if assignment_id:
        assignment = Assignment.objects.filter(id=assignment_id)
        if assignment.exists():
            assignment = assignment.first()
            if action == "student_test":
                path = assignment.student_test.url
            elif action == "zip_file":
                path = os.path.dirname(assignment.student_test.url) + "/assignment" + assignment_id + ".zip"
            elif action == "config_file":
                path = os.path.dirname(assignment.student_test.url) + "/config.json"
            elif action == "assignment_file":
                path = assignment.assignment_file.url
            else:
                return Http404
    elif submission_id:
        submission = Submission.objects.get(id=submission_id)
        if submission:
            path = submission.get_log_file()

    file_path = os.path.join(settings.MEDIA_ROOT, path)
    if os.path.exists(file_path):
        with open(file_path, 'rb') as fh:

            #url = urllib.request.pathname2url(file_path)
            #content_type = mimetypes.guess_type(url)[0]
            content_type = 'application/force-download'
            response = HttpResponse(fh.read(), content_type=content_type)
            response['Content-Disposition'] = 'inline; filename=' + os.path.basename(file_path)
            return response
    
    raise Http404


@login_required(login_url='login')
def course(request, course_id, assignment_id=0):
    user = User.objects.get(pk=request.user.id)
    student = Student.objects.filter(user=user)[0]
    course = Course.objects.get(pk=course_id)
    assignments = Assignment.objects.filter(course=course, open_date__lte=timezone.now())

    selected_assignment = None
    submission_history = None
    assignment_zip_file = None
    if (assignment_id != 0):
        selected_assignment = Assignment.objects.get(id=assignment_id, open_date__lte=timezone.now())
        submission_history = Submission.objects.filter(student=student).order_by("-publish_date")

        assignment_zip_file = os.path.split(selected_assignment.student_test.url)[0] + "/assignment" + str(assignment_id) + ".zip"

    return render(request, 'course.html', {
        'assignment_zip_file': assignment_zip_file,
        'assignment_id': int(assignment_id),
        'course': course,
        'assignments': assignments,
        'selected_assignment': selected_assignment,
        'submission_history': submission_history
    }
    )



@csrf_exempt
def api(request, action):
    email = request.POST.get('email')
    submission_pass = request.POST.get('submission_pass')

    user = User.objects.filter(email=email)
    if (user.exists()):
        student = Student.objects.filter(user=user[0], submission_pass=submission_pass)
        if (student.exists()):
            student = student[0]
            if (action == "submit_assignment"):

                if request.method == 'POST':
                    
                    assignment = Assignment.objects.get(id=request.POST.get('assignment'), open_date__lte=timezone.now())

                    if not assignment:
                        response_data = {"status": 200, "type": "SUCCESS",
                         "message": "Assignment doesn't exists"}
                    elif timezone.now() > assignment.due_date:
                        response_data = {"status": 200, "type": "SUCCESS",
                         "message": "Assignment submission date expired"}
                    else:
                        submission = Submission(submission_file=request.FILES['submission_file'], 
                            assignment=assignment,
                            student=student)
                        submission.save()

                        submission_file_url = submission.submission_file.url
                        extract_directory = submission_file_url.replace(".zip","/")

                        zip_file = zipfile.ZipFile(submission.submission_file.url, 'r')
                        zip_file.extractall(extract_directory)
                        zip_file.close()

                        # Move Instructor Test File
                        shutil.copy(assignment.instructor_test.url, extract_directory)
                        
                        # TODO: Move Student Test File
                        #shutil.copy(assignment.student_test.url, extract_directory)                        

                        score, outlog = run_student_tests(extract_directory, assignment.total_points, assignment.timeout)
                        write_student_log(extract_directory, outlog)

                        submission.passed  = score[0]
                        submission.failed  = score[1]
                        submission.percent = score[2]

                        submission.save()

                        response_data = {"status": 200, "type": "SUCCESS",
                             "message": score}

                else:
                    response_data = {"status": 400, "type": "ERROR",
                             "message": "Use POST method"}
            else:
                response_data = {"status": 400, "type": "ERROR",
                             "message": "Invalid action"}
        else:
            response_data = {"status": 400, "type": "ERROR",
                             "message": "Invalid student"}
    else:
        response_data = {"status": 400,
                         "type": "ERROR", "message": "Invalid user"}

    return JsonResponse(response_data, safe=False)
