# python lib imports
import logging
import datetime
import enum
import threading
import time
import json
import string
import secrets
import subprocess
import functools
import inspect
import uuid
import socket
import os
import shlex

# flask and related imports
from flask import (
    Flask,
    render_template,
    url_for,
    redirect,
    flash,
    request,
    abort,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import aliased
from sqlalchemy import Index, JSON, desc, and_
from flask_migrate import Migrate
from flask_login import (
    LoginManager,
    UserMixin,
    login_user,
    logout_user,
    login_required,
    current_user,
)
from flask_admin import Admin
from flask_admin.actions import action
from flask_admin.contrib.sqla import ModelView
from werkzeug.security import generate_password_hash, check_password_hash
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SelectField, TextAreaField, SubmitField
from wtforms.validators import DataRequired, Email, Length
from flask_babel import Babel, gettext
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from markupsafe import Markup
import waitress
from authlib.integrations.flask_client import OAuth
from werkzeug.middleware.proxy_fix import ProxyFix

# virtualization interfaces
import docker
import libvirt
import openstack
from cinderclient import client as cinderclient

# other 3rd party imports
import argh
import humanize
import pytz

logging.basicConfig(level=logging.DEBUG)

try:
    cmd = "git describe --tags --always --dirty"
    version = subprocess.check_output(shlex.split(cmd)).decode().strip()
except:
    logging.exception("Couldn't get git version: ")
    version = ""

try:
    cmd = "hostname"
    hostname = subprocess.check_output(shlex.split(cmd)).decode().strip()
except Exception:
    logging.exception("Couldn't get hostname: ")
    hostname = ""


app = Flask(__name__)
app.config["SECRET_KEY"] = "your_secret_key"
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get(
    "ADA2025_SQLALCHEMY_URL", "sqlite:///app.db"
)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
login_manager = LoginManager(app)
login_manager.login_view = "login"
admin = Admin(
    url="/flaskyadmin",
    template_mode="bootstrap4",
)
admin.init_app(app)

limiter = Limiter(
    # no default limit because flask-admin trips it up
    # instead we put 60 per minute on all requests, except
    # /login and /register, which have 60 per hour
    get_remote_address,
    app=app,
    storage_uri="memory://",
    strategy="fixed-window",
)


# for nicer formatting of json data in flask-admin forms
class JsonTextAreaField(TextAreaField):
    def process_formdata(self, valuelist):
        if valuelist:
            value = valuelist[0]
            if value:
                try:
                    self.data = json.loads(value)
                except ValueError:
                    self.data = None
                    raise ValueError(self.gettext("Invalid JSON data."))
            else:
                self.data = None
        else:
            self.data = None

    def _value(self):
        if self.data is not None:
            return json.dumps(self.data, indent=4)
        else:
            return ""


# make the flask-admin interface only accessible to admins
class ProtectedModelView(ModelView):
    def is_accessible(self):
        if not (current_user.is_authenticated and current_user.is_admin):
            abort(403)
        else:
            return True

    def inaccessible_callback(self, name, **kwargs):
        return redirect(url_for("welcome"))

    @action(
        "clone", "Clone", "Are you sure you want to create a copy of the selected rows?"
    )
    def action_clone(self, ids):
        try:
            for id in ids:
                record = self.get_one(id)
                if record is not None:
                    clone = self._create_clone(record)
                    self.session.add(clone)
            self.session.commit()
            flash(f"Successfully created a copy of {len(ids)} records.")
        except Exception as ex:
            if not self.handle_view_exception(ex):
                raise
            flash("Failed to clone record. %(error)s", "error", error=str(ex))

    def _create_clone(self, record):
        clone = self.model()
        for field in self._get_field_names():
            if field != "id":
                setattr(clone, field, getattr(record, field))
        return clone

    def _get_field_names(self):
        return self.model.__table__.columns.keys()


socket.setdefaulttimeout(5)


def get_hostname(ip):
    try:
        hostname, _, _ = socket.gethostbyaddr(ip)
        return hostname
    except Exception:
        logging.Exception(f"Couldn't get hostname of {ip}")
        return ""


thread_id_map = {}
thread_id_counter = 0
thread_id_lock = threading.Lock()


def get_small_thread_id():
    global thread_id_counter, thread_id_map, thread_id_lock
    thread_id = threading.get_ident()
    with thread_id_lock:
        if thread_id not in thread_id_map:
            thread_id_map[thread_id] = thread_id_counter
            thread_id_counter += 1
    return thread_id_map[thread_id]


# decorator for functions that logs some stuff
def log_function_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start_time = time.perf_counter()
        function_signature = inspect.signature(func)
        bound_arguments = function_signature.bind(*args, **kwargs)
        bound_arguments.apply_defaults()

        call_uuid = uuid.uuid4().hex[:8]  # Generate a short unique ID
        small_thread_id = get_small_thread_id()  # Get a small, unique thread ID
        logging.info(
            f"[{call_uuid}-{small_thread_id}] Entering function '{func.__name__}' with bound arguments {bound_arguments.arguments}"
        )

        result = func(*args, **kwargs)

        elapsed_time = time.perf_counter() - start_time
        logging.info(
            f"[{call_uuid}-{small_thread_id}] Exiting function '{func.__name__}' after {elapsed_time:.6f} seconds"
        )
        return result

    return wrapper


# Association table for many-to-many relationship between User and Machine (shared_users)
shared_user_machine = db.Table(
    "shared_user_machine",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id")),
    db.Column("machine_id", db.Integer, db.ForeignKey("machine.id")),
)
# Association table
user_data_source_association = db.Table(
    "user_data_source",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id")),
    db.Column("data_source_id", db.Integer, db.ForeignKey("data_source.id")),
)


def gen_token(length):
    """
    Generate a cryptographically secure alphanumeric string of the given length.
    """
    alphabet = string.ascii_letters + string.digits
    secure_string = "".join(secrets.choice(alphabet) for _ in range(length))
    return secure_string


class User(db.Model, UserMixin):
    """
    User model, also used for flask-login
    """

    id = db.Column(db.Integer, primary_key=True)
    is_enabled = db.Column(db.Boolean, nullable=False, default=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    username = db.Column(db.String(100), unique=True, nullable=False)
    password_hash = db.Column(db.String(200))
    given_name = db.Column(db.String(100))
    family_name = db.Column(db.String(100))
    organization = db.Column(db.String(200))
    job_title = db.Column(db.String(200))
    email = db.Column(db.String(200), unique=True, nullable=False)
    language = db.Column(db.String(5), default="en", nullable=False)
    timezone = db.Column(db.String(50), default="Europe/London", nullable=False)

    # oauth2 stuff
    provider = db.Column(db.String(64))  # e.g. 'google', 'local'
    provider_id = db.Column(db.String(64))  # e.g. Google's unique ID for the user

    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    group_id = db.Column(db.Integer, db.ForeignKey("group.id"))
    group = db.relationship("Group", back_populates="users")
    owned_machines = db.relationship(
        "Machine", back_populates="owner", foreign_keys="Machine.owner_id"
    )
    shared_machines = db.relationship(
        "Machine", secondary=shared_user_machine, back_populates="shared_users"
    )
    data_sources = db.relationship(
        "DataSource", secondary=user_data_source_association, back_populates="users"
    )
    data_transfer_jobs = db.relationship("DataTransferJob", back_populates="user")
    problem_reports = db.relationship("ProblemReport", back_populates="user")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<{self.username}>"


class ProtectedUserModelView(ProtectedModelView):
    column_list = (
        "id",
        "is_enabled",
        "username",
        "given_name",
        "family_name",
        "organization",
        "job_title",
        "email",
        "group",
        "is_admin",
        "creation_date",
    )
    form_columns = (
        "is_enabled",
        "username",
        "password",
        "given_name",
        "family_name",
        "organization",
        "job_title",
        "language",
        "timezone",
        "email",
        "group",
        "is_admin",
        "creation_date",
        "owned_machines",
        "shared_machines",
        "data_sources",
        "data_transfer_jobs",
    )
    column_searchable_list = ("username", "email")
    column_sortable_list = ("id", "username", "email", "creation_date")
    column_filters = ("is_enabled", "is_admin", "group")
    column_auto_select_related = True
    form_extra_fields = {"password": PasswordField("Password")}

    def on_model_change(self, form, model, is_created):
        if form.password.data:
            model.set_password(form.password.data)


class DataSource(db.Model):
    """
    The DataSource model represents a source of data for users that
    they can use to copy into their machine.

    This is done by SSHing into the source_host and then running
    rsync to sync the data into the machine ip.
    """

    id = db.Column(db.Integer, primary_key=True)
    source_host = db.Column(db.String, nullable=False)
    source_dir = db.Column(db.String, nullable=False)
    data_size = db.Column(db.Integer, nullable=False)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    users = db.relationship(
        "User", secondary=user_data_source_association, back_populates="data_sources"
    )
    data_transfer_jobs = db.relationship(
        "DataTransferJob", back_populates="data_source"
    )

    def __repr__(self):
        return f"<{self.source_host}:{self.source_dir}>"


class ProtectedDataSourceModelView(ProtectedModelView):
    column_list = (
        "id",
        "source_host",
        "source_dir",
        "data_size",
        "creation_date",
        "users",
    )
    form_columns = (
        "source_host",
        "source_dir",
        "data_size",
        "users",
        "data_transfer_jobs",
    )
    column_searchable_list = ("source_host", "source_dir")
    column_sortable_list = ("id", "source_host", "data_size", "creation_date")
    column_filters = ("source_host", "source_dir", "data_size")
    column_auto_select_related = True


Index("source_host_source_dir_idx", DataSource.source_host, DataSource.source_dir)


class DataTransferJobState(enum.Enum):
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    HIDDEN = "HIDDEN"


class DataTransferJob(db.Model):
    """
    The DataTransferJob tracks a copy from a DataSource into a Machine
    """

    id = db.Column(db.Integer, primary_key=True)
    state = db.Column(db.Enum(DataTransferJobState), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    data_source_id = db.Column(db.Integer, db.ForeignKey("data_source.id"))
    machine_id = db.Column(db.Integer, db.ForeignKey("machine.id"))
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    finish_date = db.Column(db.DateTime, default=datetime.datetime.utcnow)

    user = db.relationship("User", back_populates="data_transfer_jobs")
    data_source = db.relationship("DataSource", back_populates="data_transfer_jobs")
    machine = db.relationship("Machine", back_populates="data_transfer_jobs")
    problem_reports = db.relationship(
        "ProblemReport", back_populates="data_transfer_job"
    )

    def __repr__(self):
        return f"<DTJob {self.id}>"


class ProtectedDataTransferJobModelView(ProtectedModelView):
    column_list = (
        "id",
        "state",
        "user",
        "data_source",
        "machine",
        "creation_date",
        "finish_date",
    )
    form_columns = ("state", "user", "data_source", "machine")
    column_searchable_list = ("state",)
    column_sortable_list = ("id", "state", "creation_date", "finish_date")
    column_filters = ("state", "user", "data_source", "machine")
    column_auto_select_related = True


class Group(db.Model):
    """
    A group that users belong to. A user can belong to a single group

    The group determines which MachineTemplates a user can see.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )

    users = db.relationship("User", back_populates="group")
    machine_templates = db.relationship("MachineTemplate", back_populates="group")

    def __repr__(self):
        return f"<{self.name}>"


class ProtectedGroupModelView(ProtectedModelView):
    column_list = ("id", "name", "creation_date", "users", "machine_templates")
    form_columns = ("name", "users", "machine_templates")
    column_searchable_list = ("name",)
    column_sortable_list = ("id", "name", "creation_date")
    column_filters = ("name",)
    column_auto_select_related = True


class MachineProvider(db.Model):
    """
    A machine provider is a local connection like local docker
    or external provider like openstack or cloud provider like aws
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(100), nullable=False)
    customer = db.Column(db.String(100), nullable=False)
    provider_data = db.Column(JSON, nullable=False)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    machine_templates = db.relationship(
        "MachineTemplate", back_populates="machine_provider"
    )

    def __repr__(self):
        return f"<{self.name}>"

    """
    based on experience, cloud providers have the following parameters:

    azure:
      resource_group: test
      instance_type: Standard_E8s_v3
      vm_image: UbuntuLTS
    openstack-1:
      flavor: climb.group
      network_uuid: 895d68df-6cff-45a1-9399-c10109b8bfbd
      key_name: denis
      vol_size: 120
      vol_image: e09bc162-1e18-447c-a577-e6b8af2cbc61
    gcp:
      zone: europe-west2-c
      image_family: ubuntu-1804-lts
      image_project: ubuntu-os-cloud
      machine_type: n1-highmem-4
      boot_disk_size: 120GB
    aws:
      image_id: ami-0c30afcb7ab02233d
      instance_type: r5.xlarge
      key_name: awstest
      security_group_id: sg-002bd90eab458665f
      subnet_id: subnet-ffd5a396
    oracle:
      compartment_id: ocid1.compartment.oc1..aaaaaaaao4kpjckz2pjmlc...
      availability_domain: LfHB:UK-LONDON-1-AD-1
      image_id: ocid1.image.oc1.uk-london-1.aaaaaaaaoc2hx6m45bba2av...
      shape: VM.Standard2.4
      subnet_id: ocid1.subnet.oc1.uk-london-1.aaaaaaaab3zsfqtkoyxtx...
      boot_volume_size_in_gbs: 120
    """


class ProtectedMachineProviderModelView(ProtectedModelView):
    column_list = ("id", "name", "type", "customer", "provider_data")
    column_searchable_list = ("name", "type", "customer")
    column_filters = ("name", "customer")

    form_columns = (
        "name",
        "type",
        "customer",
        "creation_date",
        "provider_data",
        "machine_templates",
    )
    column_auto_select_related = True

    # Custom formatter for provider_data
    def _provider_data_formatter(view, context, model, name):
        json_data = model.provider_data
        if not json_data:
            return ""
        formatted_data = [f"{k}: {v}" for k, v in json_data.items()]
        return Markup("<br>".join(formatted_data))

    column_formatters = {
        "provider_data": _provider_data_formatter,
    }

    def scaffold_form(self):
        form_class = super(ProtectedMachineProviderModelView, self).scaffold_form()
        form_class.provider_data = JsonTextAreaField("Provider Data")
        return form_class


class MachineTemplate(db.Model):
    """
    A MachineTemplate is a template from which the user builds Machines
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    type = db.Column(db.String(100), nullable=False)
    image = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(200), nullable=True)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    memory_limit_gb = db.Column(db.Integer, nullable=True)
    cpu_limit_cores = db.Column(db.Integer, nullable=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    group = db.relationship("Group", back_populates="machine_templates")
    machine_provider_id = db.Column(
        db.Integer, db.ForeignKey("machine_provider.id"), nullable=False
    )
    machine_provider = db.relationship(
        "MachineProvider", back_populates="machine_templates"
    )
    machines = db.relationship("Machine", back_populates="machine_template")
    extra_data = db.Column(db.JSON)

    def __repr__(self):
        return f"<{self.name}>"


class ProtectedMachineTemplateModelView(ProtectedModelView):
    column_list = (
        "id",
        "name",
        "type",
        "image",
        "memory_limit_gb",
        "cpu_limit_cores",
        "group",
        "machines",
        "extra_data",
    )
    form_columns = (
        "name",
        "type",
        "image",
        "description",
        "cpu_limit_cores",
        "memory_limit_gb",
        "group",
        "machines",
        "extra_data",
    )
    column_searchable_list = ("name", "type", "image")
    column_sortable_list = (
        "id",
        "name",
        "type",
        "image",
        "creation_date",
        "memory_limit_gb",
        "cpu_limit_cores",
    )
    column_filters = ("type", "group")
    column_auto_select_related = True

    # Custom formatter for extra_data
    def _extra_data_formatter(view, context, model, name):
        json_data = model.extra_data
        if not json_data:
            return ""
        formatted_data = [f"{k}: {v}" for k, v in json_data.items()]
        return Markup("<br>".join(formatted_data))

    column_formatters = {
        "extra_data": _extra_data_formatter,
    }

    def scaffold_form(self):
        form_class = super(ProtectedMachineTemplateModelView, self).scaffold_form()
        form_class.extra_data = JsonTextAreaField("Extra Data")
        return form_class


class MachineState(enum.Enum):
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    FAILED = "FAILED"
    DELETING = "DELETING"
    DELETED = "DELETED"


class Machine(db.Model):
    """
    A Machine represents a container or virtual machine that the user
    uses.
    """

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    ip = db.Column(db.String(45), nullable=False)
    hostname = db.Column(db.String(200), default="")
    token = db.Column(db.String(16), nullable=False, default=lambda: gen_token(16))
    state = db.Column(db.Enum(MachineState), nullable=False, index=True)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    machine_template_id = db.Column(db.Integer, db.ForeignKey("machine_template.id"))

    owner = db.relationship(
        "User", back_populates="owned_machines", foreign_keys=[owner_id]
    )
    shared_users = db.relationship(
        "User", secondary=shared_user_machine, back_populates="shared_machines"
    )
    machine_template = db.relationship("MachineTemplate", back_populates="machines")
    data_transfer_jobs = db.relationship("DataTransferJob", back_populates="machine")
    problem_reports = db.relationship("ProblemReport", back_populates="machine")

    def __repr__(self):
        return f"<{self.name}>"


class ProtectedMachineModelView(ProtectedModelView):
    column_list = (
        "id",
        "name",
        "ip",
        "hostname",
        "token",
        "state",
        "creation_date",
        "owner",
        "machine_template",
        "shared_users",
    )
    form_columns = (
        "name",
        "ip",
        "hostname",
        "token",
        "state",
        "owner",
        "shared_users",
        "machine_template",
        "data_transfer_jobs",
    )
    column_searchable_list = ("name", "ip", "token", "state")
    column_sortable_list = ("id", "name", "ip", "state", "creation_date")
    column_filters = ("state", "owner", "machine_template")
    column_auto_select_related = True


class ProblemReport(db.Model):
    """
    Represents a user's problem report. It always has a user and
    may also be associated with a machine or data transfer job.

    The reports are meant to be shown to admins on the admin page
    """

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(100), nullable=False)
    description = db.Column(db.Text, nullable=False)
    creation_date = db.Column(
        db.DateTime, default=datetime.datetime.utcnow, nullable=False
    )
    is_hidden = db.Column(db.Boolean, nullable=False, default=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    user = db.relationship("User", back_populates="problem_reports")
    machine_id = db.Column(db.Integer, db.ForeignKey("machine.id"))
    machine = db.relationship("Machine", back_populates="problem_reports")
    data_transfer_job_id = db.Column(db.Integer, db.ForeignKey("data_transfer_job.id"))
    data_transfer_job = db.relationship(
        "DataTransferJob", back_populates="problem_reports"
    )


class ProtectedProblemReportModelView(ProtectedModelView):
    column_list = (
        "title",
        "description",
        "creation_date",
        "is_hidden",
        "user",
        "machine",
        "data_transfer_job",
    )
    column_searchable_list = ("title", "description")
    column_filters = ("is_hidden", "user", "machine", "data_transfer_job")
    form_columns = (
        "title",
        "description",
        "is_hidden",
        "user",
        "machine",
        "data_transfer_job",
    )


# add flask-sqlalchemy views to flask-admin
admin.add_view(ProtectedUserModelView(User, db.session))
admin.add_view(ProtectedDataSourceModelView(DataSource, db.session))
admin.add_view(ProtectedDataTransferJobModelView(DataTransferJob, db.session))
admin.add_view(ProtectedGroupModelView(Group, db.session))
admin.add_view(ProtectedMachineProviderModelView(MachineProvider, db.session))
admin.add_view(ProtectedMachineTemplateModelView(MachineTemplate, db.session))
admin.add_view(ProtectedMachineModelView(Machine, db.session))
admin.add_view(ProtectedProblemReportModelView(ProblemReport, db.session))


# This is used in base.jinja2 to build the side bar menu
def get_main_menu():
    return [
        {
            "icon": "house",
            "name": gettext("Welcome page"),
            "href": "/",
        },
        {
            "icon": "cubes",
            "name": gettext("Machines"),
            "href": "/machines",
        },
        {
            "icon": "database",
            "name": gettext("Data"),
            "href": "/data",
        },
        {
            "icon": "book",
            "name": gettext("Citations"),
            "href": "/citations",
        },
        {
            "icon": "gear",
            "name": gettext("Settings"),
            "href": "/settings",
        },
        {
            "icon": "lightbulb",
            "name": gettext("Help"),
            "href": "/help",
        },
        {
            "icon": "circle-question",
            "name": gettext("About"),
            "href": "/about",
        },
        {
            "icon": "toolbox",
            "name": gettext("Admin"),
            "href": "/admin",
            "admin_only": True,
        },
    ]


def icon(text):
    """
    Return html for fontawesome icon - solid variant.
    """
    return f'<i class="fas fa-fw fa-{ text }"></i>'


def icon_regular(text):
    """
    Return html for fontawesome icon - regular variant.
    """
    return f'<i class="far fa-fw fa-{ text }"></i>'


def email(addr):
    """
    Return html for email link with fontawesome icon.
    """
    return (
        f'<a href="mailto:{ addr }"><i class="fas fa-fw fa-envelope"></i> { addr }</a>'
    )


def external_link(addr, desc=None):
    """
    Return html for link with external-link fontawesome icon.
    """
    if not desc:
        desc = addr
    return f'<a target="_blank" href="{ addr }">{ desc } <i class="fas fa-fw fa-external-link"></i></a>'


def info(text, **kwargs):
    """
    Return html for paragraph with info icon and some text - accepts kwargs
    """
    paragraph = f'<p><i class="fas fa-fw fa-info-circle"></i> {text}</p>'
    return paragraph.format(**kwargs)


def idea(text, **kwargs):
    """
    Return html for paragraph with info lightbulb and some text - accepts kwargs
    """
    paragraph = f'<p><i class="fas fa-fw fa-lightbulb"></i> {text}</p>'
    return paragraph.format(**kwargs)


@app.errorhandler(403)
def forbidden_handler(e):
    t = gettext("Access denied")
    m = gettext("Sorry, you don't have access to that page or resource.")

    return render_template("error.jinja2", message=m, title=t, code=403), 403


# 404 error handler
@app.errorhandler(404)
def notfound_handler(e):
    t = gettext("Not found")
    m = gettext("Sorry, that page or resource could not be found.")

    return render_template("error.jinja2", message=m, title=t, code=404), 404


# 429 error handler
@app.errorhandler(429)
def toomanyrequests_handler(e):
    t = gettext("Too many requests")
    m = gettext(
        "Sorry, you're making too many requests. Please wait a while and then try again."
    )

    return render_template("error.jinja2", message=m, title=t, code=429), 429


# 500 error handler
@app.errorhandler(500)
def applicationerror_handler(e):
    t = gettext("Application error")
    m = gettext("Sorry, the application encountered a problem.")

    return render_template("error.jinja2", message=m, title=t, code=500), 500


@app.context_processor
def inject_globals():
    """Add some stuff into all templates."""
    return {
        "icon": icon,
        "icon_regular": icon_regular,
        "email": email,
        "external_link": external_link,
        "info": info,
        "idea": idea,
        "main_menu": get_main_menu(),
        "humanize": humanize,
        "time_now": int(time.time()),
        "version": version,
        "hostname": hostname,
    }


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        start_time = time.time()

        def custom_start_response(status, response_headers, exc_info=None):
            end_time = time.time()
            duration = end_time - start_time
            request_log = f"waitress: {request.remote_addr} {duration:.4f}s {request.method} {status} {request.path}"
            logging.info(request_log)
            return start_response(status, response_headers, exc_info)

        return self.app(environ, custom_start_response)


app.wsgi_app = ProxyFix(
    RequestLoggingMiddleware(app.wsgi_app), x_for=1, x_proto=1, x_host=1
)


@login_manager.user_loader
def load_user(user_id):
    """
    This is called by flask-login on every request to load the user
    """
    return User.query.filter_by(id=int(user_id)).first()


def get_locale():
    if current_user and current_user.is_authenticated:
        if current_user.language:
            return current_user.language
    lang = request.accept_languages.best_match(["en", "zh", "sl"])
    logging.info(f"language best match: {lang}")
    return lang


def get_timezone():
    if current_user and current_user.is_authenticated:
        return current_user.timezone


babel = Babel(app, locale_selector=get_locale, timezone_selector=get_timezone)


class LoginForm(FlaskForm):
    username = StringField(
        gettext("Username"), validators=[DataRequired(), Length(min=2, max=32)]
    )
    password = PasswordField(
        gettext("Password"), validators=[DataRequired(), Length(min=8, max=100)]
    )
    submit = SubmitField("Sign In")


oauth = OAuth(app)

if os.environ.get("GOOGLE_OAUTH2_CLIENT_ID"):
    google = oauth.register(
        name="google",
        client_id=os.environ.get("GOOGLE_OAUTH2_CLIENT_ID"),
        client_secret=os.environ.get("GOOGLE_OAUTH2_CLIENT_SECRET"),
        access_token_url="https://accounts.google.com/o/oauth2/token",
        access_token_params=None,
        authorize_url="https://accounts.google.com/o/oauth2/auth",
        authorize_params=None,
        api_base_url="https://www.googleapis.com/oauth2/v1/",
        userinfo_endpoint="https://openidconnect.googleapis.com/v1/userinfo",
        client_kwargs={"scope": "email profile"},
    )


@app.route("/google_login")
@limiter.limit("60 per hour")
def google_login():
    # send the users to google to log in. once they're logged
    # in, they're sent back to google_authorize, where we
    # make an account for them from the data that google
    # provided
    google = oauth.create_client("google")
    redirect_uri = url_for("google_authorize", _external=True)
    return google.authorize_redirect(redirect_uri)


def gen_unique_username(email, max_attempts=1000):
    username = ""
    attempt = 0
    email_prefix = email.split("@")[0]
    # TODO remove bad characters from prefix, allow a-Z, 0-9, and .
    while True:
        if not email:
            username = gen_token(16)
        elif not username:
            username = email_prefix
        else:
            username = email_prefix + "_" + gen_token(4)

        if not User.query.filter_by(username=username).first():
            return username

        if attempt > max_attempts // 2:
            username = gen_token(24)
        if attempt > max_attempts:
            abort(500)

        attempt = attempt + 1


@app.route("/google_authorize")
@limiter.limit("60 per hour")
def google_authorize():
    # google has authenticated the user and sent them back
    # here, make an account if they don't have one and log
    # them in
    try:
        google = oauth.create_client("google")
        google.authorize_access_token()
        resp = google.get("userinfo")
        if resp.status_code != 200:
            raise Exception("Failed to get user info")
        user_info = resp.json()

        user = User.query.filter_by(email=user_info.get("email")).first()

        # Update or create the user
        if user:
            # Update user info if needed
            user.given_name = user_info.get("given_name", user.given_name)
            user.family_name = user_info.get("family_name", user.family_name)
            user.provider_id = user_info.get("id", user.provider_id)
        else:
            # Create a new user
            user = User(
                username=gen_unique_username(user_info.get("email", "")),
                given_name=user_info.get("given_name", ""),
                family_name=user_info.get("family_name", ""),
                email=user_info.get("email", ""),
                provider="google",
                provider_id=user_info.get("id", ""),
                language="en",  # TODO: google gives locale, handle it here
                timezone="Europe/London",
            )
            db.session.add(user)

        db.session.commit()

        if user and user.is_enabled and user.group:
            # Log the user in
            login_user(user)
            return redirect(url_for("index"))
        else:
            # Show activation message
            flash(
                gettext(
                    "Your account has been created, but it has to be activated by staff, which typically happens within 24 hours. As soon as it's activated, you'll be able to log in using Google. We appreciate your patience."
                )
            )
            return redirect(url_for("login"))

    except Exception as e:
        # Log the error and show an error message
        app.logger.error(e)
        flash(
            gettext(
                "An error occurred while processing your Google login. Please try again."
            )
        )
        return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
@limiter.limit("60 per hour")
def login():
    """
    Login page and login logic
    """

    # show the google login button or not
    show_google_button = False
    if os.environ.get("GOOGLE_OAUTH2_CLIENT_ID"):
        show_google_button = True

    form = LoginForm()

    # log out users who go to the login page
    if current_user.is_authenticated:
        logout_user()
        flash(gettext("You've been logged out."))
        return render_template(
            "login.jinja2",
            title="Login",
            form=form,
            show_google_button=show_google_button,
        )

    # POST path
    if request.method == "POST":
        if form.validate_on_submit():
            user = User.query.filter_by(username=form.username.data).first()

            # oauth2 returning users
            if user and user.provider_id:
                if user.provider == "google":
                    # google users
                    return redirect(url_for("google_login"))
                else:
                    flash(gettext("Invalid provider"), "danger")
                    return render_template(
                        "login.jinja2",
                        title="Login",
                        form=form,
                        show_google_button=show_google_button,
                    )

            # oauth2 users trying to log in locally but don't have a password
            if user and user.provider != "local" and not user.password_hash:
                flash(
                    gettext(
                        "Sorry, you can't use a local login. Try using the login method you signed in (eg. Google) with the first time, or contact support for help.",
                        "danger",
                    )
                )
                return render_template(
                    "login.jinja2",
                    title="Login",
                    form=form,
                    show_google_button=show_google_button,
                )

            # local users
            if user and user.check_password(form.password.data):
                # pw ok ut account not activated
                if not user.is_enabled:
                    flash(
                        gettext(
                            "Account not activated. If it's been more than 24 hours please contact support."
                        ),
                        "danger",
                    )
                    return render_template(
                        "login.jinja2",
                        title="Login",
                        form=form,
                        show_google_button=show_google_button,
                    )

                # log user in
                login_user(user)
                flash(gettext("Logged in successfully."), "success")
                return redirect(url_for("index"))
            else:
                flash(gettext("Invalid username or password."), "danger")
        else:
            logging.warning(f"wtforms didn't validate form: { form.errors }")
            # technically it's not invalid but don't give that away
            flash(gettext("Invalid username or password."), "danger")

    # GET path
    return render_template(
        "login.jinja2",
        title="Login",
        form=form,
        show_google_button=show_google_button,
    )


class RegistrationForm(FlaskForm):
    username_min = 2
    username_max = 32
    username = StringField(
        gettext("Username"),
        validators=[DataRequired(), Length(min=username_min, max=username_max)],
    )
    password_min = 8
    password_max = 100
    password = PasswordField(
        gettext("Password"),
        validators=[DataRequired(), Length(min=password_min, max=password_max)],
    )
    given_name_min = 2
    given_name_max = 100
    given_name = StringField(
        gettext("Given Name"),
        validators=[DataRequired(), Length(min=given_name_min, max=given_name_max)],
    )
    family_name_min = 2
    family_name_max = 100
    family_name = StringField(
        gettext("Family Name"),
        validators=[DataRequired(), Length(min=family_name_min, max=family_name_max)],
    )
    language = SelectField(
        gettext("Language"), validators=[DataRequired()], choices=["en", "zh", "sl"]
    )
    timezone = SelectField(
        gettext("Timezone"), validators=[DataRequired()], choices=pytz.all_timezones
    )
    email_min = 4
    email_max = 200
    email = StringField(
        gettext("Email"),
        validators=[DataRequired(), Email(), Length(min=email_min, max=email_max)],
    )
    organization_min = 2
    organization_max = 200
    organization = StringField(
        gettext("Organization"),
        validators=[DataRequired(), Length(min=organization_min, max=organization_max)],
    )
    job_title_min = 2
    job_title_max = 200
    job_title = StringField(
        gettext("Job Title"),
        validators=[DataRequired(), Length(min=job_title_min, max=job_title_max)],
    )
    submit = SubmitField(gettext("Register"))


@app.route("/register", methods=["GET", "POST"])
@limiter.limit("60 per hour")
def register():
    form = RegistrationForm()

    if request.method == "POST":
        if form.validate_on_submit():
            if form.language.data not in form.language.choices:
                abort(404)
            if form.timezone.data not in form.timezone.choices:
                abort(404)

            new_user = User(
                username=form.username.data,
                given_name=form.given_name.data,
                family_name=form.family_name.data,
                email=form.email.data,
                provider="local",
                provider_id="",
                language=form.language.data,
                timezone=form.timezone.data,
                organization=form.organization.data,
                job_title=form.job_title.data,
            )
            new_user.set_password(form.password.data)

            # Give new users admin's data sources TODO: remove this
            new_user.data_sources = User.query.filter_by(id=1).first().data_sources

            db.session.add(new_user)
            db.session.commit()

            flash(
                gettext(
                    "Thank you for registering. You will be emailed when your account is activated"
                )
            )
            return redirect(url_for("login"))
        else:
            flash(gettext("Sorry, the form could not be validated."))

    return render_template(
        "register.jinja2",
        form=form,
        title=gettext("Register account"),
    )


@app.route("/")
@limiter.limit("60 per minute")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("welcome"))
    else:
        return redirect(url_for("login"))


@app.route("/logout")
@limiter.limit("60 per minute")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/welcome")
@limiter.limit("60 per minute")
@login_required
def welcome():
    return render_template(
        "welcome.jinja2",
        title=gettext("Welcome page"),
        ProblemReport=ProblemReport,
        now=datetime.datetime.utcnow(),
    )


@app.route("/machines")
@limiter.limit("60 per minute")
@login_required
def machines():
    """
    The machine page displays and controls the user's machines
    """
    return render_template(
        "machines.jinja2",
        title=gettext("Machines"),
        MachineTemplate=MachineTemplate,
        MachineState=MachineState,
        Machine=Machine,
        now=datetime.datetime.utcnow(),
        machine_format_dtj=machine_format_dtj,
    )


def contains_non_alphanumeric_chars(string):
    # or -
    for char in string:
        if not char.isalnum() and char != "-":
            return True


@app.route("/rename_machine", methods=["POST"])
@limiter.limit("60 per minute")
@login_required
def rename_machine():
    machine_id = request.form.get("machine_id")
    machine_new_name = request.form.get("machine_new_name")
    if not machine_new_name or not machine_id:
        flash(gettext("Invalid values for machine rename"), "danger")
        return redirect(url_for("machines"))

    if len(machine_new_name) <= 3 or len(machine_new_name) > 80:
        logging.error(f"len(machine_new_name) = {len(machine_new_name)}")
        flash(
            gettext(
                "New name must be between 4 and 80 characters long, and can contain characters a-Z,0-9 and -"
            ),
            "danger",
        )
        return redirect(url_for("machines"))

    if contains_non_alphanumeric_chars(machine_new_name):
        flash(
            gettext(
                "New name contains non-alphanumeric characters. Characters that are allowed are a-Z,0-9 and -"
            ),
            "danger",
        )
        return redirect(url_for("machines"))

    try:
        machine_id = int(machine_id)
    except:
        flash(gettext("Invalid values for machine rename"), "danger")
        return redirect(url_for("machines"))

    machine = Machine.query.filter_by(id=machine_id).first()

    if Machine.query.filter_by(name=machine_new_name).first():
        flash(gettext("The requested machine name is already taken"), "danger")
        return redirect(url_for("machines"))

    # TODO: check if the new name includes a username other than CU.name

    old_name = machine.name
    machine.name = machine_new_name
    db.session.commit()

    flash(f"Machine {old_name} renamed to {machine_new_name}")
    return redirect(url_for("machines"))


@app.route("/settings")
@limiter.limit("60 per minute")
@login_required
def settings():
    return render_template(
        "settings.jinja2", title=gettext("Settings"), threading=threading
    )


@app.route("/admin")
@limiter.limit("60 per minute")
@login_required
def admin():
    if not current_user.is_admin:
        abort(403)
    return render_template(
        "admin.jinja2",
        title=gettext("Admin"),
        User=User,
        Machine=Machine,
    )


@app.route("/citations")
@limiter.limit("60 per minute")
@login_required
def citations():
    return render_template("citations.jinja2", title=gettext("Citations"))


@app.route("/about")
@limiter.limit("60 per minute")
@login_required
def about():
    return render_template("about.jinja2", title=gettext("About"))


@app.route("/help")
@limiter.limit("60 per minute")
@login_required
def help():
    return render_template("help.jinja2", title=gettext("Help"))


class ProblemReportForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired()])
    description = TextAreaField("Description", validators=[])
    machine_name = StringField("machine_name", validators=[DataRequired()])
    data_transfer_job_id = StringField("data_transfer_job_id", validators=[])
    submit = SubmitField("Submit")


@app.route("/report_problem", methods=["GET", "POST"])
@limiter.limit("60 per minute")
@login_required
def report_problem():
    form = ProblemReportForm()
    if request.method == "POST":
        if form.validate_on_submit():
            machine_name = form.machine_name.data
            data_transfer_job_id = form.data_transfer_job_id.data

            machine = Machine.query.filter_by(name=machine_name).first()
            data_transfer_job = DataTransferJob.query.filter_by(
                id=data_transfer_job_id
            ).first()

            problem_report = ProblemReport(
                title=form.title.data,
                description=form.description.data,
                user=current_user,
                machine=machine,
                data_transfer_job=data_transfer_job,
            )
            db.session.add(problem_report)
            db.session.commit()
            flash(gettext("Problem report submitted successfully."), "success")
            return redirect(url_for("index"))
    else:
        form.machine_name.data = request.args.get("machine_name")
        form.data_transfer_job_id.data = request.args.get("data_transfer_job_id")
        form.title.data = request.args.get("title")
        return render_template(
            "report_problem.jinja2",
            title=gettext("Help"),
            form=form,
        )


def encode_date_time(date_time):
    """
    Encode the date and time into a 6 character string
    """
    base_chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    base = len(base_chars)

    # Encode year, month, day, hour, minute, and second separately
    encoded_parts = []
    encoded_parts.append(
        base_chars[date_time.year % 100 // 4]
    )  # Encoded year (4-year granularity)
    encoded_parts.append(
        base_chars[date_time.month - 1]
    )  # Encoded month (0-based index)
    encoded_parts.append(base_chars[date_time.day - 1])  # Encoded day (0-based index)
    encoded_parts.append(base_chars[date_time.hour])  # Encoded hour
    encoded_parts.append(base_chars[date_time.minute])  # Encoded minute
    encoded_parts.append(base_chars[date_time.second])  # Encoded second

    # Combine encoded parts into a single string
    encoded_date_time = "".join(encoded_parts)
    return encoded_date_time


def mk_safe_machine_name(username):
    """
    We need a unique name in some circumstances, so we use the username
    and encoded datetime.

    This assumes the user doesn't want to make more than 1 machine
    per second
    """
    machine_name = username + "-" + encode_date_time(datetime.datetime.utcnow())
    return machine_name


class DataTransferForm(FlaskForm):
    data_source = SelectField(
        gettext("Data Source"), validators=[DataRequired()], coerce=int
    )
    machine = SelectField(gettext("Machine"), validators=[DataRequired()], coerce=int)
    submit = SubmitField(gettext("Submit"))


@app.route("/dismiss_datatransferjob", methods=["POST"])
@limiter.limit("60 per minute")
@login_required
def dismiss_datatransferjob():
    """
    Endpoint for hiding the data transfer job from the data page
    by setting its state to HIDDEN
    """
    job_id = request.form.get("job_id")
    if not job_id:
        abort(404)

    job = DataTransferJob.query.filter_by(id=job_id).first_or_404()
    job.state = DataTransferJobState.HIDDEN
    db.session.commit()
    return "OK"


def machine_format_dtj(machine):
    """
    Returns a set of unique formatted data transfer job entries for a specific machine.
    """
    Source = aliased(DataSource)
    jobs = (
        DataTransferJob.query.join(Source, DataTransferJob.data_source)
        .filter(
            and_(
                DataTransferJob.machine == machine,
                DataTransferJob.state == DataTransferJobState.DONE,
            )
        )
        .with_entities(Source.source_host, Source.source_dir)
        .distinct()
    )

    return {f"{source_host}:{source_dir}" for source_host, source_dir in jobs}


@app.route("/data", methods=["GET", "POST"])
@limiter.limit("60 per minute")
@login_required
def data():
    if current_user.is_admin:
        # the admin can see everything
        data_sources = DataSource.query.all()
        machines = Machine.query.filter_by(state=MachineState.READY)
    else:
        # a normal user can see their own stuff
        data_sources = current_user.data_sources
        machines = current_user.owned_machines + current_user.shared_machines

    # fill in the form select options
    form = DataTransferForm()
    form.data_source.choices = [
        (ds.id, f"{ds.source_host}:{ds.source_dir} ({ds.data_size} MB)")
        for ds in data_sources
    ]
    form.machine.choices = [
        (m.id, m.name) for m in machines if m.state == MachineState.READY
    ]

    if request.method == "POST":
        if form.validate_on_submit():
            machine = Machine.query.filter_by(id=form.machine.data).first()
            data_source = DataSource.query.filter_by(id=form.data_source.data).first()

            if not machine or not data_source:
                abort(404)

            if machine not in machines or data_source not in data_sources:
                abort(403)

            # security checks ok

            job = DataTransferJob(
                state=DataTransferJobState.RUNNING,
                user=current_user,
                data_source=data_source,
                machine=machine,
            )
            db.session.add(job)
            db.session.flush()
            db.session.commit()
            threading.Thread(target=start_data_transfer, args=(job.id,)).start()

            flash(gettext("Starting data transfer. Refresh page to update status."))
            return redirect(url_for("data"))
        else:
            flash(gettext("The data transfer job submission could not be validated."))
            return redirect(url_for("data"))
    else:
        sorted_jobs = (
            DataTransferJob.query.filter(DataTransferJob.user_id == current_user.id)
            .filter(DataTransferJob.state != DataTransferJobState.HIDDEN)
            .order_by(desc(DataTransferJob.id))
            .all()
        )
        return render_template(
            "data.jinja2",
            title=gettext("Data"),
            form=form,
            sorted_jobs=sorted_jobs,
        )


@log_function_call
def do_rsync(source_host, source_dir, dest_host, dest_dir):
    try:
        # Construct the rsync command
        rsync_cmd = (
            f"rsync -avz -e 'ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null' "
            f"{source_dir} {dest_host}:{dest_dir}"
        )
        logging.info(rsync_cmd)

        # Construct the ssh command to run the rsync command on the source_host
        ssh_cmd = (
            f"ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null "
            f'{source_host} "{rsync_cmd}"'
        )
        logging.info(ssh_cmd)

        # Execute the ssh command
        subprocess.run(ssh_cmd, shell=True, check=True, stderr=subprocess.PIPE)

        logging.info("Data transfer completed successfully.")
        return True

    except Exception as e:
        logging.exception("Error occurred during data transfer: ")
        return False


@log_function_call
def start_data_transfer(job_id):
    """
    Thread function that takes a job and runs the data transfer
    """
    with app.app_context():
        job = DataTransferJob.query.filter_by(id=job_id).first()
        if not job:
            logging.error(f"job {job_id} not found!")

        result = do_rsync(
            "dv@" + job.data_source.source_host,
            job.data_source.source_dir,
            "ubuntu@" + job.machine.ip,
            "",
        )

        job.finish_time = datetime.datetime.utcnow()
        if result:
            job.state = DataTransferJobState.DONE
        else:
            job.state = DataTransferJobState.FAILED
        db.session.commit()


@app.route("/share_machine/<machine_id>")
@limiter.limit("60 per minute")
@login_required
def share_machine(machine_id):
    """
    Shows the share page
    """
    machine_id = int(machine_id)
    machine = Machine.query.filter_by(id=machine_id).first_or_404()

    return render_template("share.jinja2", title=gettext("Machines"), machine=machine)


@app.route("/share_accept/<machine_token>")
@limiter.limit("60 per hour")
@login_required
def share_accept(machine_token):
    """
    This is the endpoint hit by the user accepting a share
    """
    machine = Machine.query.filter_by(token=machine_token).first_or_404()
    if current_user == machine.owner:
        flash(gettext("You own that machine."))
        return redirect(url_for("machines"))
    if current_user in machine.shared_users:
        flash(gettext("You already have that machine."))
        return redirect(url_for("machines"))

    machine.shared_users.append(current_user)
    db.session.commit()
    flash(gettext("Shared machine has been added to your account."))
    return redirect(url_for("machines"))


@app.route("/share_revoke/<machine_id>")
@limiter.limit("60 per hour")
@login_required
def share_revoke(machine_id):
    """
    The owner revokes all shares. We do this by removing shared_users
    and resetting the machine token
    """
    machine = Machine.query.filter_by(id=machine_id).first_or_404()
    if current_user != machine.owner:
        flash(gettext("You can't revoke shares on a machine you don't own.", "danger"))
        return redirect(url_for("machines"))

    machine.token = gen_token(16)
    machine.shared_users = []
    db.session.commit()
    flash(
        gettext(
            "Shares for machine have been removed and a new share link has been generated"
        )
    )
    return redirect(url_for("machines"))


@app.route("/new_machine", methods=["POST"])
@limiter.limit(
    "100 per day, 10 per minute, 1/2 seconds", key_func=lambda: current_user.username
)
@login_required
def new_machine():
    """
    Launches thread to create the container/vm
    """
    machine_template_name = request.form.get("machine_template_name", "")

    machine_name = mk_safe_machine_name(current_user.username)

    mt = MachineTemplate.query.filter_by(name=machine_template_name).first_or_404()

    m = Machine(
        name=machine_name,
        ip="",
        state=MachineState.PROVISIONING,
        owner=current_user,
        shared_users=[],
        machine_template=mt,
    )
    db.session.add(m)
    db.session.commit()

    logging.warning("starting new machine thread")

    if mt.type == "docker":
        threading.Thread(target=docker_start_container, args=(m.id,)).start()
    elif mt.type == "libvirt":
        threading.Thread(target=libvirt_start_vm, args=(m.id,)).start()
    elif mt.type == "openstack":
        threading.Thread(target=openstack_start_vm, args=(m.id,)).start()

    flash(
        gettext("Creating machine in the background. Refresh page to update status."),
        category="success",
    )
    return redirect(url_for("machines"))


@app.route("/stop_machine", methods=["POST"])
@limiter.limit("60 per minute")
@login_required
def stop_machine():
    """
    Start thread to stop machine
    """

    # sanity checks
    machine_id = request.form.get("machine_id")
    if not machine_id:
        logging.warning(f"machine_id parameter missing: {machine_id}")
        abort(404)
    machine_id = int(machine_id)
    machine = Machine.query.filter_by(id=machine_id).first_or_404()
    if not current_user.is_admin and not current_user == machine.owner:
        logging.error(
            f"user {current_user.id} is not the owner of machine {machine_id} nor admin"
        )
        abort(403)
    if machine.state in [
        MachineState.PROVISIONING,
        MachineState.DELETED,
        MachineState.DELETING,
    ]:
        logging.warning(
            f"machine {machine_id} is not in correct state for deletion: {machine.state}"
        )

    # good to go
    logging.info(f"deleting machine with machine id {machine_id}")
    machine.state = MachineState.DELETING
    db.session.commit()

    if machine.machine_template.type == "docker":
        threading.Thread(target=docker_stop_container, args=(machine_id,)).start()
    elif machine.machine_template.type == "libvirt":
        threading.Thread(target=libvirt_stop_vm, args=(machine.id,)).start()
    elif machine.machine_template.type == "openstack":
        threading.Thread(target=openstack_stop_vm, args=(machine.id,)).start()

    flash(gettext("Deleting machine"), category="success")
    return redirect(url_for("machines"))


@log_function_call
def openstack_conn_from_mp(mp):
    auth_url = mp.provider_data.get("auth_url")
    user_domain_name = mp.provider_data.get("user_domain_name")
    project_domain_name = mp.provider_data.get("project_domain_name")
    username = mp.provider_data.get("username")
    password = mp.provider_data.get("password")
    project_name = mp.provider_data.get("project_name")

    conn = openstack.connection.Connection(
        auth_url=auth_url,
        username=username,
        password=password,
        project_name=project_name,
        project_domain_name=project_domain_name,
        user_domain_name=user_domain_name,
    )
    return conn


@log_function_call
def openstack_wait_for_vm_ip(conn, server_id, network_uuid, timeout=300):
    start_time = time.time()
    server = None

    # Get the network name using the network UUID
    network = conn.network.get_network(network_uuid)
    network_name = network.name

    while time.time() - start_time < timeout:
        server = conn.compute.get_server(server_id)
        addresses = server.addresses.get(network_name, [])

        for address in addresses:
            if address.get("OS-EXT-IPS:type") == "fixed":
                return address.get("addr")

        time.sleep(5)

    raise TimeoutError(
        f"Server '{server_id}' did not get an IP address within {timeout} seconds."
    )


def openstack_wait_for_volume(
    auth_url,
    username,
    password,
    project_name,
    user_domain_name,
    project_domain_name,
    volume_id,
    timeout=1200,
):
    # Set environment variables for OpenStack CLI
    env = {
        "OS_AUTH_URL": auth_url,
        "OS_USERNAME": username,
        "OS_PASSWORD": password,
        "OS_PROJECT_NAME": project_name,
        "OS_USER_DOMAIN_NAME": user_domain_name,
        "OS_PROJECT_DOMAIN_NAME": project_domain_name,
    }

    start_time = time.time()

    while True:
        # Get volume details in JSON format
        volume_details_output = subprocess.check_output(
            ["openstack", "volume", "show", volume_id, "-f", "json"], env=env
        )
        volume_details = json.loads(volume_details_output)

        # Check volume status
        if volume_details["status"] == "available":
            logging.info(f"Volume {volume_id} is available.")
            return volume_details

        # Check if timeout has been reached
        if time.time() - start_time > timeout:
            logging.info(
                f"Timeout reached while waiting for volume {volume_id} to become available."
            )
            return None

        logging.info(
            f"Volume {volume_id} is not available yet. Retrying in 5 seconds..."
        )
        time.sleep(5)


def openstack_wait_for_vm_state(
    auth_url,
    username,
    password,
    project_name,
    user_domain_name,
    project_domain_name,
    server_id,
    state,
    timeout=300,
):
    # Set environment variables for OpenStack CLI
    env = {
        "OS_AUTH_URL": auth_url,
        "OS_USERNAME": username,
        "OS_PASSWORD": password,
        "OS_PROJECT_NAME": project_name,
        "OS_USER_DOMAIN_NAME": user_domain_name,
        "OS_PROJECT_DOMAIN_NAME": project_domain_name,
    }

    start_time = time.time()

    while True:
        # Get server details in JSON format
        server_details_output = subprocess.check_output(
            ["openstack", "server", "show", server_id, "-f", "json"], env=env
        )
        server_details = json.loads(server_details_output)

        # Check server status
        if server_details["status"] == state:
            logging.info(f"Server {server_id} is {state}.")
            return server_details

        # Check if timeout has been reached
        if time.time() - start_time > timeout:
            logging.info(
                f"Timeout reached while waiting for server {server_id} to become {state}."
            )
            return None

        logging.info(f"Server {server_id} is not {state} yet. Retrying in 5 seconds...")
        time.sleep(5)


# Function to create a new VM from an image
@log_function_call
def openstack_start_vm(m_id):
    with app.app_context():
        try:
            m = Machine.query.filter_by(id=m_id).first()
            mt = m.machine_template
            mp = mt.machine_provider

            vm_name = m.name
            flavor_name = mt.extra_data.get("flavor_name")
            network_uuid = mt.extra_data.get("network_uuid")
            vol_size = mt.extra_data.get("vol_size")
            security_groups = mt.extra_data.get("security_groups", [])
            vol_image = mt.image

            conn = openstack_conn_from_mp(mp)

            # Find the network by UUID
            network = conn.network.get_network(network_uuid)
            if not network:
                logging.error(f"Network with UUID '{network_uuid}' not found.")
                return

            # Find the flavor by name
            flavor = conn.compute.find_flavor(flavor_name)
            if not flavor:
                logging.error(f"Flavor '{flavor_name}' not found.")
                return

            # Create a bootable volume from the specified image
            cinder = cinderclient.Client("3", session=conn.session)
            image = conn.compute.find_image(vol_image)
            if not image:
                logging.error(f"Image '{vol_image}' not found.")
                return

            volume = cinder.volumes.create(
                size=vol_size,
                imageRef=image.id,
                name=f"{vm_name}_boot",
            )

            auth_url = mp.provider_data.get("auth_url")
            user_domain_name = mp.provider_data.get("user_domain_name")
            project_domain_name = mp.provider_data.get("project_domain_name")
            username = mp.provider_data.get("username")
            password = mp.provider_data.get("password")
            project_name = mp.provider_data.get("project_name")

            openstack_wait_for_volume(
                auth_url,
                username,
                password,
                project_name,
                user_domain_name,
                project_domain_name,
                volume.id,
            )

            # Create the server (VM)
            server = conn.compute.create_server(
                name=vm_name,
                flavor_id=flavor.id,
                networks=[{"uuid": network_uuid}],
                security_groups=security_groups,
                block_device_mapping_v2=[
                    {
                        "boot_index": "0",
                        "uuid": volume.id,
                        "source_type": "volume",
                        "destination_type": "volume",
                        "delete_on_termination": True,
                    }
                ],
            )

            openstack_wait_for_vm_state(
                auth_url,
                username,
                password,
                project_name,
                user_domain_name,
                project_domain_name,
                server.id,
                state="ACTIVE",
                timeout=300,
            )

            logging.info(f"Server '{server.name}' created with ID: {server.id}")

            # wait for ip
            m.ip = openstack_wait_for_vm_ip(conn, server.id, network.id)
            m.hostname = get_hostname(m.ip)
            m.state = MachineState.READY

            db.session.commit()

        except:
            logging.exception("Couldn't start openstack vm: ")
            m.state = MachineState.FAILED
            db.session.commit()


@log_function_call
def openstack_get_vm_by_ip(conn, target_ip):
    servers = conn.compute.servers()

    for server in servers:
        addresses = server.addresses
        for network, network_addresses in addresses.items():
            for address in network_addresses:
                ip = address.get("addr")
                if ip == target_ip:
                    return server

    return None


@log_function_call
def openstack_stop_vm(m_id):
    with app.app_context():
        try:
            m = Machine.query.filter_by(id=m_id).first()
            mt = m.machine_template
            mp = mt.machine_provider

            auth_url = mp.provider_data.get("auth_url")
            user_domain_name = mp.provider_data.get("user_domain_name")
            project_domain_name = mp.provider_data.get("project_domain_name")
            username = mp.provider_data.get("username")
            password = mp.provider_data.get("password")
            project_name = mp.provider_data.get("project_name")

            env = {
                "OS_AUTH_URL": auth_url,
                "OS_USERNAME": username,
                "OS_PASSWORD": password,
                "OS_PROJECT_NAME": project_name,
                "OS_USER_DOMAIN_NAME": user_domain_name,
                "OS_PROJECT_DOMAIN_NAME": project_domain_name,
            }

            try:
                subprocess.check_output(
                    ["openstack", "server", "delete", m.name], env=env
                )
            except Exception:
                logging.exception("Problem deleting openstack vm: ")

            m.state = MachineState.DELETED
            db.session.commit()
        except:
            logging.exception("Couldn't stop openstack vm: ")
            m.state = MachineState.FAILED
            db.session.commit()


@log_function_call
def docker_get_container_by_ip(client, ip_address, network):
    try:
        network = client.networks.get(network)
        containers = network.containers

        # Search for the container with the specified IP address
        container = None
        for cont in containers:
            cont_ips = [
                x["IPAddress"]
                for x in cont.attrs["NetworkSettings"]["Networks"].values()
            ]
            if ip_address in cont_ips:
                container = cont
                break

        if container is None:
            logging.error("container with ip {ip_address} not found")
            return

        container_id = container.id
        container = client.containers.get(container_id)
        return container
    except docker.errors.APIError as e:
        logging.exception("Error getting container by IP address")
    except Exception as e:
        logging.exception("Error: Unknown error occurred")


@log_function_call
def docker_get_ip(client, container_name, network):
    container = client.containers.get(container_name)
    maybe_ip = container.attrs["NetworkSettings"]["Networks"][network]["IPAddress"]
    return maybe_ip


@log_function_call
def docker_wait_for_ip(client, container_name, network):
    while not (ip := docker_get_ip(client, container_name, network)):
        time.sleep(1)
    return ip


@log_function_call
def docker_stop_container(machine_id):
    with app.app_context():
        machine = Machine.query.filter_by(id=machine_id).first()
        mt = machine.machine_template
        mp = mt.machine_provider

        network = mp.provider_data.get("network")
        machine_ip = machine.ip
        docker_base_url = mp.provider_data.get("base_url")
        client = docker.DockerClient(docker_base_url)

        try:
            container = docker_get_container_by_ip(client, machine.ip, network)
            if container:
                container.stop()
        except docker.errors.APIError as e:
            logging.exception("Error: stopping and removing container")
        except Exception as e:
            logging.exception("Error: Unknown error occurred")

        machine.state = MachineState.DELETED
        db.session.commit()
    logging.info(f"deleted container with machine id {machine_id}")


@log_function_call
def docker_start_container(m_id):
    logging.warning("entered docker_start_container thread")
    with app.app_context():
        try:
            m = Machine.query.filter_by(id=m_id).first()
            mt = m.machine_template
            mp = mt.machine_provider

            network = mp.provider_data.get("network")
            cpu_cores = mt.cpu_limit_cores
            mem_limit_gb = mt.memory_limit_gb

            docker_base_url = mp.provider_data.get("base_url")
            client = docker.DockerClient(docker_base_url)

            cpu_period = 100000
            cpu_quota = int(cpu_period * cpu_cores)
            mem_limit = f"{mem_limit_gb * 1024}m"  # Convert GB to MB

            # Define container options, including CPU and memory limits
            container_options = {
                "name": m.name,
                "image": mt.image,
                "network": network,
                "cpu_period": cpu_period,
                "cpu_quota": cpu_quota,
                "mem_limit": mem_limit,
            }
            logging.info(json.dumps(container_options, indent=4))

            # Start the container
            container = client.containers.run(
                **container_options,
                detach=True,
            )

            m.ip = docker_wait_for_ip(client, m.name, network)

            m.state = MachineState.READY
            db.session.commit()
        except:
            logging.exception("Error: ")
            try:
                container.stop()
            except:
                logging.exception("Error: ")
            try:
                container.remove()
            except:
                logging.exception("Error: ")

            m.state = MachineState.FAILED
            m.ip = ""
            db.session.commit()

    logging.warning("all done!")


@log_function_call
def libvirt_get_vm_ip(conn, vm_name):
    try:
        domain = conn.lookupByName(vm_name)
    except libvirt.libvirtError:
        logging.error(f"Error: VM '{vm_name}' not found.")
        return None

    interfaces = domain.interfaceAddresses(
        libvirt.VIR_DOMAIN_INTERFACE_ADDRESSES_SRC_LEASE
    )

    for _, interface in interfaces.items():
        for address in interface["addrs"]:
            if address["type"] == libvirt.VIR_IP_ADDR_TYPE_IPV4:
                logging.info(f"vm {vm_name} acquired ip: {address['addr']}")
                return address["addr"]

    return None


@log_function_call
def libvirt_wait_for_ip(conn, vm_name):
    logging.info("waiting for libvirt_wait_for_ip")
    while not (ip := libvirt_get_vm_ip(conn, vm_name)):
        time.sleep(1)
    return ip


@log_function_call
def libvirt_wait_for_vm(conn, vm_name):
    # Wait for the virtual machine to be in the running state
    try:
        domain = conn.lookupByName(vm_name)
    except libvirt.libvirtError:
        logging.error(f"Error: VM '{vm_name}' not found.")
        return

    while True:
        state, _ = domain.state()
        if state == libvirt.VIR_DOMAIN_RUNNING:
            logging.info(f"Virtual machine {vm_name} is now running")
            break
        time.sleep(1)


@log_function_call
def libvirt_start_vm(m_id):
    """
    Start a vm and wait for it to have an ip
    """
    logging.info("entered start_libvirt_vm thread")
    with app.app_context():
        try:
            m = Machine.query.filter_by(id=m_id).first()
            mt = m.machine_template
            mp = mt.machine_provider

            qemu_url = mp.provider_data.get("base_url")

            name = m.name
            image = mt.image
            cores = mt.cpu_limit_cores
            mem = int(mt.memory_limit_gb) * 1024 * 1024

            # TODO rewrite the following to use python api
            # clone vm
            subprocess.run(
                [
                    "virt-clone",
                    "--connect",
                    qemu_url,
                    "--original",
                    image,
                    "--name",
                    name,
                    "--auto-clone",
                ]
            )

            # Set the CPU and memory limits
            subprocess.run(
                [
                    "virsh",
                    "--connect",
                    qemu_url,
                    "setvcpus",
                    name,
                    str(cores),
                    "--config",
                    "--maximum",
                ]
            )
            subprocess.run(
                [
                    "virsh",
                    "--connect",
                    qemu_url,
                    "setvcpus",
                    name,
                    str(cores),
                    "--config",
                ]
            )
            subprocess.run(
                [
                    "virsh",
                    "--connect",
                    qemu_url,
                    "setmaxmem",
                    name,
                    str(mem),
                    "--config",
                ]
            )
            subprocess.run(
                ["virsh", "--connect", qemu_url, "setmem", name, str(mem), "--config"]
            )

            # start vm
            subprocess.run(["virsh", "--connect", qemu_url, "start", name])

            conn = libvirt.open(qemu_url)
            logging.info(f"waiting for vm {name} to come up")
            libvirt_wait_for_vm(conn, name)
            logging.info(f"vm {name} is up, waiting for ip")
            ip = libvirt_wait_for_ip(conn, name)
            logging.info(f"vm {name} has acquired an ip: {ip}")

            m.ip = ip
            m.state = MachineState.READY
            db.session.commit()
        except:
            logging.exception("Error creating libvirt vm: ")
            m.state = MachineState.FAILED
            db.session.commit()


@log_function_call
def libvirt_stop_vm(m_id):
    with app.app_context():
        m = Machine.query.filter_by(id=m_id).first()
        try:
            mt = m.machine_template
            mp = mt.machine_provider
            vm_name = m.name
            qemu_base_url = mp.provider_data.get("base_url")

            # Create a new connection to a local libvirt session
            conn = libvirt.open(qemu_base_url)

            # Stop the virtual machine
            domain = conn.lookupByName(vm_name)
            domain.destroy()

            # Delete the disk
            storage_paths = []
            for pool in conn.listAllStoragePools():
                for vol in pool.listAllVolumes():
                    if vm_name in vol.name():
                        storage_paths.append(vol.path())
                        vol.delete(0)

            domain.undefine()
            conn.close()
        except:
            logging.exception("Error stopping libvirt vm:")
        m.state = MachineState.DELETED
        db.session.commit()

    logging.info(f"Stopped virtual machine {vm_name} and deleted its disk")


def create_initial_db():
    # add an admin user and test machinetemplate and machine
    with app.app_context():
        if not User.query.filter_by(username="denis").first():
            logging.warning("Creating default data.")
            demo_source1 = DataSource(
                source_host="localhost",
                source_dir="/tmp/demo1",
                data_size="123",
            )
            demo_source2 = DataSource(
                source_host="localhost",
                source_dir="/tmp/demo2",
                data_size="321",
            )
            demo_source3 = DataSource(
                source_host="localhost",
                source_dir="/tmp/demo3",
                data_size="432",
            )

            admin_group = Group(name="admins")
            normal_user_group = Group(name="XRAY scientists")

            admin_user = User(
                is_enabled=True,
                username="admin",
                given_name="Admin",
                family_name="Admin",
                group=admin_group,
                language="zh",
                is_admin=True,
                email="admin@ada.stfc.ac.uk",
                data_sources=[demo_source1, demo_source2],
            )
            admin_password = gen_token(16)
            logging.info(f"Created user: username: admin password: {admin_password}")
            admin_user.set_password(admin_password)
            normal_user = User(
                is_enabled=True,
                username="xrayscientist",
                given_name="John",
                family_name="Smith",
                group=normal_user_group,
                language="zh",
                is_admin=False,
                email="xrays.smith@llnl.gov",
                data_sources=[demo_source2, demo_source3],
            )
            normal_user_password = gen_token(16)
            normal_user.set_password(normal_user_password)
            logging.info(
                f"Created user: username: xrayscientist password: {normal_user_password}"
            )

            docker_machine_provider = MachineProvider(
                name="Local docker",
                type="docker",
                customer="unknown",
                provider_data={
                    "base_url": "unix:///var/run/docker.sock",
                    "network": "adanet",
                },
            )
            libvirt_machine_provider = MachineProvider(
                name="Local libvirt",
                type="libvirt",
                customer="unknown",
                provider_data={
                    "base_url": "qemu:///system",
                },
            )
            stfc_os_machine_provider = MachineProvider(
                name="STFC OpenStack",
                type="openstack",
                customer="unknown",
                provider_data={
                    "auth_url": "https://openstack.stfc.ac.uk:5000/v3",
                    "user_domain_name": "stfc",
                    "project_domain_name": "Default",
                    "username": "gek25866",
                    "password": "",
                    "project_name": "IDAaaS-Dev",
                },
            )

            test_machine_template1 = MachineTemplate(
                name="Muon analysis template",
                type="libvirt",
                memory_limit_gb=16,
                cpu_limit_cores=4,
                image="debian11-5",
                group=admin_group,
                machine_provider=libvirt_machine_provider,
                description="This is a libvirt machine template that's added by default when you're running in debug mode. It references the image \"debian11-5\"",
            )
            test_machine_template2 = MachineTemplate(
                name="XRAY analysis template",
                type="docker",
                memory_limit_gb=16,
                cpu_limit_cores=4,
                image="workspace",
                group=normal_user_group,
                machine_provider=docker_machine_provider,
                description="This is a docker machine template that's added by default when you're running in debug mode. It references the image \"workspace\"",
            )
            test_machine_template3 = MachineTemplate(
                name="STFC test template",
                type="openstack",
                memory_limit_gb=32,
                cpu_limit_cores=8,
                image="denis_dev_20230511",
                group=normal_user_group,
                machine_provider=stfc_os_machine_provider,
                description="This is a STFC openstack template that's added by default when you're running in debug mode.",
                extra_data={
                    "flavor_name": "c2.large",
                    "network_uuid": "5be315b7-7ebd-4254-97fe-18c1df501538",
                    "vol_size": "200",
                    "has_https": True,
                    "security_groups": [
                        {"name": "HTTP"},
                        {"name": "HTTPS"},
                        {"name": "SSH"},
                    ],
                },
            )

            db.session.add(admin_group)
            db.session.add(admin_user)
            db.session.add(normal_user_group)
            db.session.add(normal_user)
            db.session.add(libvirt_machine_provider)
            db.session.add(docker_machine_provider)
            db.session.add(test_machine_template3)
            db.session.add(test_machine_template2)
            db.session.add(test_machine_template1)
            db.session.commit()


def clean_up_db():
    """
    Because threads are used for background tasks, anything that
    was running when the application closed will be interrupted.

    This function sets any database entries that were running into
    a failed state.

    We could also try to recover or restart some things.
    """
    with app.app_context():
        for m in Machine.query.all():
            if m.state == MachineState.PROVISIONING:
                logging.warning(f"Setting machine {m.name} to FAILED state")
                m.state = MachineState.FAILED
                # TODO: make sure all machine resources are deleted
        for j in DataTransferJob.query.all():
            if j.state == DataTransferJobState.RUNNING:
                logging.warning(f"Setting DataTransferJob {j.id} to FAILED state")
                j.state = DataTransferJobState.FAILED
                # Could also restart?
        db.session.commit()


def main(debug=False):
    create_initial_db()
    clean_up_db()

    if debug:
        app.run(debug=True)
    else:
        waitress.serve(app, host="0.0.0.0", port=5000)


if __name__ == "__main__":
    argh.dispatch_command(main)
