import os
import csv
import click
import logging
import subprocess
import shutil
import pandas as pd

from cas.ingest.ingest_user_table import ingest_user_data
from cas.flatten_data_to_tables import serialize_to_tables
from cas.file_utils import read_cas_json_file
from pathlib import Path
from ruamel.yaml import YAML


@click.group()
def cli():
    pass


@cli.command()
@click.option('-i', '--input', type=click.Path(exists=True), help='Data folder path.')
@click.option('-s', '--schema', type=click.Path(exists=True), help='Nanobot schema folder path.')
@click.option('-ct', '--curation_tables', type=click.Path(exists=True), help='TDT curation tables folder path.')
def import_data(input, schema, curation_tables):
    """
    Imports user data to the system
    Parameters:
        input (str): Path to the input data folder
    """
    global last_accession_id, accession_ids
    last_accession_id = 0
    accession_ids = list()
    new_files = list()

    user_data_path = None
    user_config_path = None
    user_cas_path = None
    for filename in os.listdir(input):
        f = os.path.join(input, filename)
        if os.path.isfile(f):
            if (filename.endswith(".tsv") or filename.endswith(".csv")) and "_std." not in filename:
                user_data_path = f
                new_files.append(user_data_path)
            elif filename.endswith(".yaml") or filename.endswith(".yml"):
                user_config_path = f
                new_files.append(user_config_path)
            elif filename.endswith(".json"):
                user_cas_path = f
                new_files.append(user_cas_path)

    # provide either json or tsv + yaml
    if user_cas_path:
        user_file_name = os.path.splitext(os.path.basename(user_cas_path))[0]
        std_data = read_cas_json_file(user_cas_path)
    else:
        if user_data_path:
            user_data_ct_path = add_user_table_to_nanobot(user_data_path, schema, curation_tables)
            new_files.append(user_data_ct_path)
        else:
            raise Exception("Couldn't find the cell type annotation config file (with yaml or yml extension) in folder: " + input)

        if user_config_path:
            user_file_name = os.path.splitext(os.path.basename(user_data_path))[0]
            std_data = ingest_user_data(user_data_path, user_config_path)
        else:
            raise Exception("Couldn't find the config data files (with yaml or yml extension) in folder: " + input)

    accession_prefix = retrieve_accession_prefix(Path(input).parent.absolute())
    std_tables = serialize_to_tables(std_data, user_file_name, input, accession_prefix)
    # std_data_path = convert_standard_json_to_table(std_data, user_file_name, input)
    for table_path in std_tables:
        user_data_ct_path = add_user_table_to_nanobot(table_path, schema, curation_tables)
        new_files.append(user_data_ct_path)

    # updated files
    new_files.append(os.path.join(schema, "table.tsv"))
    new_files.append(os.path.join(schema, "column.tsv"))

    project_folder = os.path.dirname(os.path.abspath(input))
    add_new_files_to_git(project_folder, new_files)


def add_new_files_to_git(project_folder, new_files):
    """
    Runs git add command to add imported files to the version control.
    Parameters:
        project_folder: project folder path
        new_files: imported/created file paths to add to the version control
    """
    runcmd("cd {dir} && git add {files}".
           format(dir=project_folder,
                  files=" ".join([t.replace(project_folder, ".", 1) for t in new_files])))


def add_user_table_to_nanobot(user_data_path, schema_folder, curation_tables_folder):
    """
    Adds user data to the nanobot. Adds user table to the curation tables folder and updates the nanobot table schema.
    """
    # update nanobot table.tsv
    user_data_ct_path = copy_file(user_data_path, curation_tables_folder)
    user_table_name = os.path.splitext(os.path.basename(user_data_ct_path))[0]
    table_tsv_path = os.path.join(schema_folder, "table.tsv")
    with open(table_tsv_path, 'a') as fd:
        if user_table_name.endswith("_annotation"):
            # use custom edit_view for autocomplete
            fd.write(('\n{table_name}\t{path}\t\tols_form\t').format(table_name=user_table_name, path=user_data_ct_path))
        else:
            fd.write(('\n{table_name}\t{path}\t\t\t').format(table_name=user_table_name, path=user_data_ct_path))

    user_data_extension = os.path.splitext(user_data_ct_path)[1]
    user_headers = []
    user_data = dict()
    if user_data_extension == ".tsv":
        user_headers, user_data = read_tsv_to_dict(user_data_ct_path, generated_ids=True)
    elif user_data_extension == ".csv":
        user_headers, user_data = read_csv_to_dict(user_data_ct_path, generated_ids=True)

    # update nanobot column.tsv
    column_tsv_path = os.path.join(schema_folder, "column.tsv")
    with open(column_tsv_path, 'a') as fd:
        for index, header in enumerate(user_headers):
            if header == "cell_set_accession":
                fd.write("\n" + user_table_name + "\t" + normalize_column_name(header) + "\t" +
                         header.replace("_", " ").strip() + "\t\tword\tprimary\t")
            elif index == 0 and "cell_set_accession" not in user_headers:
                fd.write("\n" + user_table_name + "\t" + normalize_column_name(header) + "\t" +
                         header.strip() + "\t\tword\tprimary\t")
            elif header == "cell_ontology_term_id":
                fd.write("\n" + user_table_name + "\t" + normalize_column_name(header) + "\t" +
                         header.replace("_", " ").strip() + "\tempty\tautocomplete_cl\t\t")
            elif header == "cell_ontology_term":
                fd.write("\n" + user_table_name + "\t" + normalize_column_name(header) + "\t" +
                         header.replace("_", " ").strip() + "\tempty\tontology_label\t\t")
            else:
                fd.write("\n" + user_table_name + "\t" + normalize_column_name(header) + "\t" +
                         header.replace("_", " ").strip() + "\tempty\ttext\t\t")
    return user_data_ct_path


def copy_file(source_file, target_folder):
    """
    Copies source file to the target location and applies header normalizations for nanobot compatibility.
    Nanobot column name requirement: All names must match: ^[\w_ ]+$' for to_url()
    """
    # new_data_path = shutil.copy(source_file, target_folder)
    source_file_name = os.path.basename(source_file)
    new_data_path = os.path.join(target_folder, source_file_name)

    sf = open(source_file, 'r')
    tf = open(new_data_path, 'w')

    row_num = 0
    for line in sf:
        if row_num == 0:
            source_file_extension = os.path.splitext(source_file)[1]
            if source_file_extension == ".tsv":
                headers = line.split("\t")
                new_headers = []
                for header in headers:
                    new_headers.append(normalize_column_name(header))
                tf.write("\t".join(new_headers) + "\n")
            elif source_file_extension == ".csv":
                headers = line.split(",")
                new_headers = []
                for header in headers:
                    new_headers.append(normalize_column_name(header))
                tf.write(",".join(new_headers))
        else:
            tf.write(line)
        row_num += 1

    sf.close()
    tf.close()

    return new_data_path


def normalize_column_name(column_name: str) -> str:
    """
    Normalizes column name for nanobot compatibility.
    Nanobot column name requirement: All names must match: ^[\w_ ]+$' for to_url()

    Parameters:
        column_name: current column name
    Returns:
        normalized column_name
    """
    return column_name.strip().replace("(", "_").replace(")", "_")


def read_tsv_to_dict(tsv_path, id_column=0, generated_ids=False):
    """
    Reads tsv file content into a dict. Key is the first column value and the value is dict representation of the
    row values (each header is a key and column value is the value).
    Args:
        tsv_path: Path of the TSV file
        id_column: Id column becomes the key of the dict. This column should be unique. Default value is first column.
        generated_ids: If 'True', uses row number as the key of the dict. Initial key is 0.
    Returns:
        Function provides two return values: first; headers of the table and second; the TSV content dict. Key of the
        content is the first column value and the values are dict of row values.
    """
    return read_csv_to_dict(tsv_path, id_column=id_column, delimiter="\t", generated_ids=generated_ids)


def read_csv_to_dict(csv_path, id_column=0, id_column_name="", delimiter=",", id_to_lower=False, generated_ids=False):
    """
    Reads tsv file content into a dict. Key is the first column value and the value is dict representation of the
    row values (each header is a key and column value is the value).
    Args:
        csv_path: Path of the CSV file
        id_column: Id column becomes the keys of the dict. This column should be unique. Default is the first column.
        id_column_name: Alternative to the numeric id_column, id_column_name specifies id_column by its header string.
        delimiter: Value delimiter. Default is comma.
        id_to_lower: applies string lowercase operation to the key
        generated_ids: If 'True', uses row number as the key of the dict. Initial key is 0.

    Returns:
        Function provides two return values: first; headers of the table and second; the CSV content dict. Key of the
        content is the first column value and the values are dict of row values.
    """
    records = dict()

    headers = []
    with open(csv_path) as fd:
        rd = csv.reader(fd, delimiter=delimiter, quotechar='"')
        row_count = 0
        for row in rd:
            _id = row[id_column]
            if id_to_lower:
                _id = str(_id).lower()

            if generated_ids:
                _id = row_count

            if row_count == 0:
                headers = row
                if id_column_name and id_column_name in headers:
                    id_column = headers.index(id_column_name)
            else:
                row_object = dict()
                for column_num, column_value in enumerate(row):
                    row_object[headers[column_num]] = column_value
                records[_id] = row_object

            row_count += 1

    return headers, records


def retrieve_accession_prefix(root_folder_path):
    """
    Reads project configuration from the root folder and returns the accession prefix.
    Params:
        root_folder_path: path of the project root folder.
    Returns: Accession id prefix defined in the project configuration.
    """
    for filename in os.listdir(root_folder_path):
        f = os.path.join(root_folder_path, filename)
        if os.path.isfile(f):
            if filename.endswith("_project_config.yaml"):
                ryaml = YAML(typ='safe')
                with open(f, "r") as fs:
                    try:
                        data = ryaml.load(fs)
                        if "accession_id_prefix" in data:
                            prefix = str(data["accession_id_prefix"]).strip()
                            if not prefix.endswith("_"):
                                prefix = prefix + "_"
                            return prefix
                        else:
                            return str(data["id"]).strip() + "_"
                    except Exception as e:
                        raise Exception("Yaml read failed:" + f + " " + str(e))


def runcmd(cmd):
    logging.info("RUNNING: {}".format(cmd))
    p = subprocess.Popen([cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True, universal_newlines=True)
    (out, err) = p.communicate()
    logging.info('OUT: {}'.format(out))
    if err:
        logging.error(err)
    if p.returncode != 0:
        raise Exception('Failed: {}'.format(cmd))


if __name__ == '__main__':
    cli()
